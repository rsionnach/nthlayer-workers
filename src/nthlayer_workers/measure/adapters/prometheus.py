"""Prometheus polling adapter — queries Prometheus HTTP API for SLO breaches."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from nthlayer_common.manifest import (
    ERROR_BUDGET_REMAINING,
    ManifestLoadError,
    foreign_yaml_reason,
    iter_manifest_files,
    load_manifest,
)
from nthlayer_common.manifest.models import SLODefinition as ManifestSLO

logger = logging.getLogger(__name__)


@dataclass
class SLODefinition:
    """An SLO parsed from an OpenSRM spec."""

    service: str
    slo_name: str
    slo_type: str  # "traditional" | "judgment"
    target: float
    window: str
    query: str  # PromQL query that returns the current value
    # The judgment kind (reversal_rate, calibration, ...). The breach check
    # dispatches on THIS, not on slo_name: in v2 metadata.name is
    # author-chosen and independent of spec.judgment_type, so a judgment SLO
    # called anything else would fall to the classical branch (opensrm-fxln).
    # How to read `query`'s value, chosen by _query_for when it built the
    # query. The breach rule and the query are one decision: an
    # overrides/decisions RATE must be inverted, a `slo:*:ratio` recording
    # rule must not, seconds must not be scaled at all. Keeping them apart
    # meant the four judgment types with no builder got an SLI query and the
    # inverting rule, reading a healthy 0.99 as 1.0% (opensrm-fxln).
    #
    # Deliberately has NO default. A wrong default here is silent: the value
    # still evaluates, still writes a verdict, and is simply the wrong
    # answer — the failure shape this whole bead is about.
    query_kind: str
    judgment_type: str | None = None


@dataclass
class EvaluationResult:
    """Result of evaluating a single SLO."""

    service: str
    slo_name: str
    slo_type: str
    target: float
    current_value: float
    breach: bool
    consecutive: int
    # The un-hysteresised verdict for THIS window. Persisted so the next
    # cycle can count consecutive windows without re-deriving the rule from
    # current_value and target — which needs the query_kind it does not have.
    raw_breach: bool


@dataclass(frozen=True)
class LoadedSpecs:
    """Outcome of scanning a specs directory (opensrm-fxln).

    ``parse_failures`` counts FILES that failed to load while aiming to be a
    manifest. It travels with the SLOs because everything downstream
    evaluates ``slos`` alone: without it, a service whose manifest failed to
    parse contributes nothing and is indistinguishable from one declaring no
    SLOs — so the SLOs that would have breached are simply never evaluated.
    """

    slos: list[SLODefinition] = field(default_factory=list)
    parse_failures: int = 0


def load_specs(specs_dir: Path) -> LoadedSpecs:
    """Load OpenSRM specs from a directory and extract SLO definitions.

    Uses ``load_manifest`` rather than reading YAML by hand. The previous
    implementation read ``spec.slos``, which exists only in srm/v1 — a v2
    manifest carries ``spec.slo`` and ``spec.judgment_slo``, so it yielded
    ZERO SLOs with no error and no warning (opensrm-fxln). The ecosystem
    migrated to v2 under opensrm-ih0v, so anything migrated was silently
    unmeasured.

    Going through the parser also removes two reimplementations that had
    drifted: a four-name judgment-type list that disagreed with
    the judgment SLO types, and a target normalisation that duplicated the
    0-100 convention nthlayer-common owns.
    """
    slos: list[SLODefinition] = []
    parse_failures = 0
    if not specs_dir.is_dir():
        return LoadedSpecs()

    for spec_file in iter_manifest_files(specs_dir):
        try:
            manifest = load_manifest(spec_file, suppress_deprecation_warning=True)
        except (ManifestLoadError, FileNotFoundError, ValueError, OSError) as exc:
            reason = foreign_yaml_reason(spec_file)
            if reason is not None:
                logger.debug("Ignoring non-manifest file %s: %s", spec_file, reason)
                continue
            parse_failures += 1
            logger.warning("Failed to load manifest %s: %s", spec_file, exc)
            continue

        for slo in manifest.slos:
            # Deliberately NOT slo.indicator_query. evaluate_slos' branches
            # are hard-coded to the semantics of the synthesised queries:
            # availability breaches on `current < 0.0`, meaningful only for
            # the error-budget series, and judgment inverts, right only for
            # the overrides/decisions RATIO. A manifest's own query supplies
            # an SLI instead, which would make availability unbreachable and
            # double-invert judgment. Query and breach rule travel together.
            query, query_kind = _query_for(manifest.name, slo)
            slos.append(SLODefinition(
                service=manifest.name,
                slo_name=slo.name,
                slo_type="judgment" if slo.judgment_type else "traditional",
                judgment_type=slo.judgment_type,
                target=slo.target,
                window=slo.window or "7d",
                query=query,
                query_kind=query_kind,
            ))

    return LoadedSpecs(slos=slos, parse_failures=parse_failures)


def _query_for(service: str, slo: ManifestSLO) -> tuple[str, str]:
    """PromQL for an SLO, always synthesised, never taken from the manifest.

    Returns ``(query, query_kind)``. The kind is not decoration: it is how
    evaluate_slos knows whether the number coming back is a rate to invert,
    a good-ratio SLI, or a signed error budget. Deriving it later from the
    SLO's name or type cannot work — that is precisely what broke.

    Judgment SLOs get the interim raw-metric queries below, or the
    recording-rule convention when their type has no builder. Every
    classical SLO reads the error budget generate writes for it: by ruling
    Z1 (opensrm-zo0o) each one is good/total against its objective, so one
    series and one breach rule cover them all. Generate writes no budget for
    some (D5: throughput, freshness, lag and custom; and any whose objective
    or latency bound it cannot trust), and those read no data. Always
    returns a query: an SLO reaching evaluation with none would be dropped
    silently.
    """
    name = slo.name
    if slo.judgment_type:
        # Keyed by judgment_type, not name: _JUDGMENT_SLO_QUERIES is a map of
        # judgment KINDS. Only 2 of the 5 V2_JUDGMENT_SLO_TYPES have a builder;
        # the rest fall through to the recording-rule convention below rather
        # than yielding an empty query, which Prometheus 400s and which then
        # reads as no-data (opensrm-fxln).
        builder = _JUDGMENT_SLO_QUERIES.get(slo.judgment_type)
        if builder is not None:
            kind = _JUDGMENT_QUERY_KINDS[slo.judgment_type]
            return builder(service, slo.window or "7d"), kind
    if not slo.judgment_type:
        # The selector filters to this SLO, as each series carries
        # slo=<name> (opensrm-h0at). Keyed by name alone, not by kind:
        # which SLOs have a budget is generate's decision, and reading the
        # series it wrote is how measure follows it without a second copy of
        # that rule.
        return (
            ERROR_BUDGET_REMAINING.selector(service=service, slo=name),
            "error_budget",
        )
    return f'slo:{name}:ratio{{service="{service}"}}', "ratio"


# PromQL builders keyed by judgment TYPE (spec.judgment_type), not by the
# SLO's name — in v2 the two are independent. Lambdas take (service,
# window).
_JUDGMENT_SLO_QUERIES = {
    "reversal_rate": lambda service, window: (
        f'sum(increase(gen_ai_overrides_total{{service="{service}"}}[{window}]))'
        f' / '
        f'sum(increase(gen_ai_decisions_total{{service="{service}"}}[{window}]))'
    ),
    "high_confidence_failure": lambda service, window: (
        f'sum(increase(gen_ai_overrides_hcf_total{{service="{service}"}}[{window}]))'
        f' / '
        f'sum(increase(gen_ai_decisions_total{{service="{service}",confidence_bucket="high"}}[{window}]))'
    ),
}


# What each builder's value means, parallel to _JUDGMENT_SLO_QUERIES above.
# A judgment_type absent from both maps falls through to the recording-rule
# convention and its "ratio" kind — never to an inverting rule it was not
# built for.
_JUDGMENT_QUERY_KINDS = {
    "reversal_rate": "judgment_rate",
    "high_confidence_failure": "judgment_rate",
}


def evaluation_custom_metadata(result: EvaluationResult) -> dict:
    """The verdict `metadata.custom` blob for one evaluation.

    One definition, shared by the CLI that writes it and
    count_consecutive_breaches that reads it back. They disagreed before:
    the writer stored a 0-1 rate under `current_value` beside a 0-100
    `target`, and the reader compared them (opensrm-fxln).
    """
    return {
        "slo_type": result.slo_type,
        "slo_name": result.slo_name,
        "target": result.target,
        "current_value": result.current_value,
        "breach": result.breach,
        "raw_breach": result.raw_breach,
        "consecutive": result.consecutive,
    }


async def query_prometheus(
    client: httpx.AsyncClient,
    prometheus_url: str,
    promql: str,
) -> float | None:
    """Execute a PromQL instant query and return the scalar value, or None on failure."""
    try:
        resp = await client.get(
            f"{prometheus_url}/api/v1/query",
            params={"query": promql},
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("data", {}).get("result", [])
        if not results:
            return None
        # Take the first result's value
        value_pair = results[0].get("value", [])
        if len(value_pair) < 2:
            return None
        val = float(value_pair[1])
        # NaN check (Prometheus returns "NaN" for division by zero)
        if val != val:  # NaN check
            return None
        return val
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
        logger.debug("Prometheus query failed: %s — %s", promql, exc)
        return None


async def query_firing_alerts(
    client: httpx.AsyncClient,
    prometheus_url: str,
    service: str | None = None,
) -> list[dict[str, Any]]:
    """Query Prometheus for currently firing alerts, optionally filtered by service."""
    try:
        resp = await client.get(
            f"{prometheus_url}/api/v1/alerts",
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
        alerts = data.get("data", {}).get("alerts", [])
        firing = [a for a in alerts if a.get("state") == "firing"]
        if service:
            firing = [a for a in firing if a.get("labels", {}).get("service") == service]
        return firing
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.debug("Alert query failed: %s", exc)
        return []


def count_consecutive_breaches(
    verdicts: list,
    service: str,
    slo_name: str,
) -> int:
    """Count consecutive recent evaluation verdicts with breach=True for a service/SLO.

    Verdicts should be sorted newest-first. Counts from the most recent
    backward until a non-breach is found.
    """
    count = 0
    for v in verdicts:
        custom = getattr(v.metadata, "custom", {}) or {}
        if (
            v.subject.type == "evaluation"
            and v.subject.ref == service
            and custom.get("slo_name") == slo_name
        ):
            # Read the window's own raw_breach, do NOT re-derive it. The
            # post-hysteresis `breach` flag is a catch-22 — it needs the
            # threshold already met — but re-deriving from current_value and
            # target needs the query_kind a stored verdict does not carry,
            # and the arithmetic that was here compared a 0-1 rate against a
            # 0-100 target, so it never counted a judgment window at all.
            #
            # Verdicts written before opensrm-fxln have no raw_breach and
            # stop the count. That costs one restarted hysteresis window per
            # SLO at upgrade; guessing their breach state would cost worse.
            if custom.get("raw_breach") is True:
                count += 1
            else:
                break
    return count


async def evaluate_slos(
    prometheus_url: str,
    slos: list[SLODefinition],
    verdict_store,
    hysteresis_threshold: int = 3,
) -> list[EvaluationResult]:
    """Evaluate all SLOs against Prometheus and return results.

    Uses the verdict store to determine consecutive breach count for hysteresis.
    """
    from nthlayer_common.verdicts import VerdictFilter

    results: list[EvaluationResult] = []

    # How far back to read. A flat limit=20 starved the counter: one cycle
    # over N SLOs writes N verdicts, so once a run covered 20 SLOs the
    # window held less than a single full cycle, `consecutive` could never
    # pass 1, and the threshold was unreachable — the same dead end the
    # raw_breach fix closed, arrived at by pagination instead of arithmetic.
    # It became reachable when load_specs went from yielding zero SLOs on a
    # v2 manifest to yielding all of them.
    #
    # NOT scoped with VerdictFilter.subject_service: measure writes the
    # service into subject.ref and leaves subject.service None, so that
    # filter matches nothing. Setting it now would scope the query to
    # verdicts written after this change and silently shorten history for
    # every SLO already running. count_consecutive_breaches filters on
    # ref + slo_name itself; this only has to fetch a window wide enough to
    # contain the answer.
    #
    # Wide enough = threshold cycles of every SLO in the run, plus headroom
    # for other writers of subject_type="evaluation" under this same
    # producer_system (tiering/promotion is one).
    history_limit = (hysteresis_threshold + 1) * max(len(slos), 1) * 2

    async with httpx.AsyncClient() as client:
        for slo in slos:
            current_value = await query_prometheus(client, prometheus_url, slo.query)
            if current_value is None:
                if slo.query_kind == "error_budget":
                    # Not debug: for a classical SLO this is usually not a
                    # scrape gap but a budget generate chose not to write
                    # (D5, or an objective it cannot trust), and the SLO is
                    # then never evaluated. An operator needs to see that.
                    logger.info(
                        "No error budget series for %s/%s; not evaluated "
                        "(generate writes none for throughput, freshness, lag "
                        "or custom SLOs, nor for an objective or latency bound "
                        "it cannot trust)",
                        slo.service, slo.slo_name,
                    )
                else:
                    logger.debug("No data for %s/%s, skipping", slo.service, slo.slo_name)
                continue

            # Dispatch on the kind of number the query returns, decided by
            # _query_for when it built the query. Never on the SLO's name or
            # type: in v2 metadata.name is author-chosen, and 3 of the 5
            # V2_JUDGMENT_SLO_TYPES have no builder, so both are the wrong
            # question to ask about a value's units (opensrm-fxln).
            if slo.query_kind == "judgment_rate":
                # Prometheus returns a 0.0-1.0 RATE; the target is a 0-100
                # SLI (hard rule 1), and the SLI is the rate's inverse:
                # reversal_rate 98.5 means "at least 98.5% not reversed".
                #
                # measure/worker.py:242 scales WITHOUT inverting, correctly:
                # it reads get_sli_value(indicator_query), already an SLI.
                # The inversion belongs to _query_for's synthesised
                # overrides/decisions ratio, not to judgment SLOs at large.
                sli_pct = (1.0 - current_value) * 100
                raw_breach = sli_pct < slo.target
            elif slo.query_kind == "error_budget":
                # The error budget is signed — negative is overspent.
                # The target is not consulted; it is baked into the rule.
                raw_breach = current_value < 0.0
            else:
                # "ratio", only for a judgment type with no builder: the
                # recording-rule convention's 0-1 good-ratio SLI against a
                # 0-100 target, so it scales but does not invert — the same
                # arithmetic as worker.py:242. Comparing the two conventions directly, as
                # this branch used to, breaches every window.
                raw_breach = current_value * 100 < slo.target

            # Get consecutive breach count from verdict store
            recent = verdict_store.query(VerdictFilter(
                producer_system="nthlayer-measure",
                subject_type="evaluation",
                limit=history_limit,
            ))
            # Sort newest first
            recent.sort(key=lambda v: v.timestamp, reverse=True)
            consecutive = count_consecutive_breaches(recent, slo.service, slo.slo_name)

            if raw_breach:
                consecutive += 1
            else:
                consecutive = 0

            # Hysteresis: judgment SLOs only breach after N consecutive windows
            if slo.slo_type == "judgment":
                breach = consecutive >= hysteresis_threshold
            else:
                # Traditional SLOs: Prometheus `for` duration handles hysteresis
                breach = raw_breach

            results.append(EvaluationResult(
                service=slo.service,
                slo_name=slo.slo_name,
                slo_type=slo.slo_type,
                target=slo.target,
                current_value=current_value,
                breach=breach,
                consecutive=consecutive,
                raw_breach=raw_breach,
            ))

    return results
