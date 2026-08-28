"""Prometheus polling adapter — queries Prometheus HTTP API for SLO breaches."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from nthlayer_common.manifest import (
    ManifestLoadError,
    foreign_yaml_reason,
    iter_manifest_files,
    load_manifest,
)

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
    JUDGMENT_SLO_TYPES, and a target normalisation that duplicated the
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
            query = slo.indicator_query or _query_for(manifest.name, slo)
            if query is None:
                logger.debug(
                    "No query for %s/%s; skipping", manifest.name, slo.name
                )
                continue
            slos.append(SLODefinition(
                service=manifest.name,
                slo_name=slo.name,
                slo_type="judgment" if slo.judgment_type else "traditional",
                target=slo.target,
                window=slo.window or "7d",
                query=query,
            ))

    return LoadedSpecs(slos=slos, parse_failures=parse_failures)


def _query_for(service: str, slo) -> str | None:
    """PromQL for an SLO whose manifest declared no indicator query.

    Judgment SLOs get the interim raw-metric queries below. Classical SLOs
    fall back to the recording-rule naming convention.
    """
    name = slo.name
    if slo.judgment_type:
        return _judgment_slo_query(service, name, slo.window or "7d")
    if name == "availability":
        return f'slo:error_budget:ratio{{service="{service}"}}'
    if name == "latency":
        percentile = getattr(slo, "percentile", None) or "p99"
        return f'slo:http_request_duration_seconds:{percentile}{{service="{service}"}}'
    return f'slo:{name}:ratio{{service="{service}"}}'


# PromQL builders keyed by judgment SLO name. Lambdas take (service,
# window); `_window` on calibration/feedback_latency indicates those
# queries are window-agnostic (the raw metric is already a gauge).
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
    "calibration": lambda service, _window: f'gen_ai_calibration_error{{service="{service}"}}',
    "feedback_latency": lambda service, _window: f'gen_ai_feedback_latency_seconds{{service="{service}"}}',
}


def _judgment_slo_query(service: str, slo_name: str, window: str) -> str:
    """Build PromQL query for judgment SLOs using interim raw metrics."""
    builder = _JUDGMENT_SLO_QUERIES.get(slo_name)
    if builder is None:
        # Stdlib logger (line 12) — use %-style formatting, not kwargs.
        # Pre-existing bug surfaced by y7dd R5: a manifest shipping an
        # unknown judgment SLO would have crashed in the original
        # if/elif's fallthrough warning too.
        logger.warning(
            "Unknown judgment SLO name '%s' for service '%s', no PromQL query available",
            slo_name,
            service,
        )
        return ""
    return builder(service, window)


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
            # Count consecutive windows where value exceeded target,
            # not where the final breach flag was set (which requires
            # the threshold to already be met — a catch-22).
            current = custom.get("current_value")
            target = custom.get("target")
            if current is not None and target is not None and current > target:
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

    async with httpx.AsyncClient() as client:
        for slo in slos:
            current_value = await query_prometheus(client, prometheus_url, slo.query)
            if current_value is None:
                logger.debug("No data for %s/%s, skipping", slo.service, slo.slo_name)
                continue

            # Determine if current value breaches the target
            if slo.slo_name in ("reversal_rate", "high_confidence_failure", "calibration"):
                # Judgment SLOs. Prometheus returns a 0.0-1.0 ratio; targets
                # use the canonical 0-100 percentage convention
                # (nthlayer-common CLAUDE.md hard rule 1, opensrm-5fff.1), and
                # the SLI is the INVERSE of the measured rate: reversal_rate
                # target 98.5 means "at least 98.5% of decisions not reversed".
                #
                # Comparing the raw ratio against the 0-100 target made these
                # SLOs unbreachable — 0.05 > 98.5 is never true (opensrm-fxln).
                # measure/worker.py:240 already scaled correctly; this is the
                # adapter catching up to its own sibling.
                sli_pct = (1.0 - current_value) * 100
                raw_breach = sli_pct < slo.target
            elif slo.slo_name == "feedback_latency":
                # Breach if latency exceeds target (in seconds)
                raw_breach = current_value > slo.target
            elif slo.slo_name == "availability":
                # Error budget ratio: breach if remaining budget < 0
                # target is 0.999, error_budget = 1 - ((1 - current) / (1 - target))
                raw_breach = current_value < 0.0
            elif slo.slo_name == "latency":
                # Breach if p99 exceeds target (target in ms, value in seconds)
                raw_breach = current_value > slo.target / 1000.0
            else:
                raw_breach = current_value < slo.target

            # Get consecutive breach count from verdict store
            recent = verdict_store.query(VerdictFilter(
                producer_system="nthlayer-measure",
                subject_type="evaluation",
                limit=20,
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
            ))

    return results
