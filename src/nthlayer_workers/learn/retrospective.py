"""Retrospective builder — walks verdict lineage to produce a post-incident analysis."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
import yaml
from nthlayer_common.manifest import (
    ManifestLoadError,
    extract_declared_dependencies,
    load_manifest,
)
from nthlayer_common.manifest.parser.v1 import is_srm_v1_format
from nthlayer_common.manifest.parser.v2 import is_opensrm_v2_format
from nthlayer_common.outcomes import (
    compute_financial_impact,
    estimate_decisions_in_window,
)
from nthlayer_common.verdicts.core import create, link
from nthlayer_common.verdicts.models import Verdict
from nthlayer_common.verdicts.store import VerdictFilter, VerdictStore

from nthlayer_workers.learn._trigger import resolve_trigger_service

log = structlog.get_logger(__name__)


def build_retrospective(
    incident_verdict_id: str,
    verdict_store: VerdictStore,
    specs_dir: str | None = None,
    decision_store: Any | None = None,
) -> Verdict:
    """Walk the verdict lineage from an incident verdict and produce a retrospective.

    Traverses lineage.context backwards through correlation → evaluation verdicts.
    Queries the store for all decision verdicts during the incident window.
    Computes timeline, duration, blast radius, and recommendations.
    """
    # Load incident verdict
    incident = verdict_store.get(incident_verdict_id)
    if incident is None:
        raise KeyError(f"Incident verdict not found: {incident_verdict_id}")

    incident_custom = getattr(incident.metadata, "custom", {}) or {}

    # Walk lineage backwards to find all related verdicts
    chain = verdict_store.by_lineage(incident_verdict_id, direction="up")

    # Classify verdicts by type
    evaluation_verdicts: list[Verdict] = []
    correlation_verdicts: list[Verdict] = []
    all_verdicts: list[Verdict] = [incident] + chain

    for v in chain:
        if v.subject.type == "evaluation":
            evaluation_verdicts.append(v)
        elif v.subject.type == "correlation":
            correlation_verdicts.append(v)

    # Also query for all decision verdicts during the incident time window
    incident_time = incident.timestamp
    if isinstance(incident_time, str):
        incident_time = datetime.fromisoformat(incident_time.replace("Z", "+00:00"))

    # Bound the query: incident time to 24h after (captures incident window without loading everything)
    window_end = incident_time + timedelta(hours=24)
    window_verdicts = verdict_store.query(VerdictFilter(
        from_time=incident_time,
        to_time=window_end,
        limit=500,
    ))
    seen_ids = {v.id for v in all_verdicts}
    all_verdicts.extend(v for v in window_verdicts if v.id not in seen_ids)

    # Build timeline
    timeline = _build_timeline(all_verdicts)

    # Compute duration (first evaluation to incident creation)
    duration_minutes = 0.0
    if evaluation_verdicts:
        earliest = min(_parse_ts(v.timestamp) for v in evaluation_verdicts)
        latest = _parse_ts(incident.timestamp)
        duration_minutes = (latest - earliest).total_seconds() / 60.0

    # Extract root cause from correlation verdict
    root_cause = None
    if correlation_verdicts:
        corr_custom = getattr(correlation_verdicts[0].metadata, "custom", {}) or {}
        root_causes = corr_custom.get("root_causes", [])
        if root_causes:
            root_cause = root_causes[0]

    # Compute decisions affected (count evaluation verdicts with breach=True),
    # both as the incident-wide total and broken down per service so the
    # financial-impact path attributes breaches to the right cost figure.
    breach_counts_by_service: dict[str, int] = {}
    for v in evaluation_verdicts:
        custom = getattr(v.metadata, "custom", {}) or {}
        if not custom.get("breach"):
            continue
        svc = v.subject.service or v.subject.ref or "unknown"
        breach_counts_by_service[svc] = breach_counts_by_service.get(svc, 0) + 1
    decisions_affected = sum(breach_counts_by_service.values())

    # Build blast radius from correlation and incident metadata
    blast_radius = []
    for v in correlation_verdicts:
        corr_custom = getattr(v.metadata, "custom", {}) or {}
        blast_radius.extend(corr_custom.get("blast_radius", []))
    if not blast_radius:
        blast_radius = incident_custom.get("blast_radius", [])

    # Load manifests from specs_dir once and share the result across
    # all consumers (financial_impact + declared_dependencies_by_service).
    # opensrm-jmy.21: declared_dependencies_by_service is the
    # ground-truth view of operator-declared deps that downstream
    # add_dependency recommendation logic compares against the
    # incident's observed call graph.
    loaded_specs = _load_manifests_from_specs(specs_dir)
    declared_dependencies_by_service = _extract_declared_dependencies(loaded_specs.manifests)

    # Financial impact (if specs available)
    financial_impact = _compute_financial_impact(
        blast_radius,
        duration_minutes,
        breach_counts_by_service,
        specs_dir,
        manifests_by_service=loaded_specs.manifests,
    )

    # Generate recommendations
    recommendations = _generate_recommendations(
        evaluation_verdicts, correlation_verdicts, incident_custom
    )

    # opensrm-dpws: resolve trigger_service via correlation-first /
    # subject-fallback precedence. None → key omitted (back-compat with
    # jmy.21's _add_dependency_recommendations no-rec path).
    trigger_service = resolve_trigger_service(
        [v.subject.service for v in correlation_verdicts],
        incident.subject.service,
    )

    retro_custom: dict[str, Any] = {
        "incident_verdict_id": incident_verdict_id,
        "duration_minutes": round(duration_minutes, 1),
        "decisions_affected": decisions_affected,
        "root_cause": root_cause,
        "blast_radius": [
            s.get("service", s) if isinstance(s, dict) else s
            for s in blast_radius
        ],
        "verdict_count": len(all_verdicts),
        "timeline": timeline[:20],  # Cap at 20 entries
        "financial_impact": financial_impact,
        "recommendations": recommendations,
        "declared_dependencies_by_service": declared_dependencies_by_service,
        # opensrm-oh27: always present, 0 on a clean load. An absent key
        # would read the same as "no failures", which is the ambiguity
        # the silent skip created in the first place.
        "manifest_parse_failures": loaded_specs.parse_failures,
    }
    if trigger_service is not None:
        retro_custom["trigger_service"] = trigger_service

    # Create retrospective verdict
    retro = create(
        subject={
            "type": "retrospective",
            "ref": incident_verdict_id,
            "summary": (
                f"{root_cause.get('service', 'unknown')} {root_cause.get('type', 'incident')}"
                f" — {duration_minutes:.0f}m duration, {decisions_affected} decisions degraded"
                if root_cause else
                f"Incident retrospective — {duration_minutes:.0f}m duration, {len(all_verdicts)} verdicts"
            ),
        },
        judgment={
            "action": "flag",
            "confidence": 0.9,
            "reasoning": f"{len(all_verdicts)} verdicts in chain, {duration_minutes:.0f}m duration",
        },
        producer={"system": "nthlayer-learn"},
        metadata={"custom": retro_custom},
    )
    link(retro, context=[incident_verdict_id])
    verdict_store.put(retro)

    # Write content-addressed Evaluation record if decision_store is configured
    if decision_store is not None:
        _write_evaluation_record(
            decision_store,
            incident_custom=incident_custom,
            duration_minutes=duration_minutes,
            decisions_affected=decisions_affected,
            root_cause=root_cause,
            verdict_count=len(all_verdicts),
            retro_timestamp=retro.timestamp,
        )

    return retro


def _build_timeline(verdicts: list[Verdict]) -> list[dict[str, str]]:
    """Build a chronological timeline from verdicts."""
    events = []
    for v in verdicts:
        events.append({
            "timestamp": str(v.timestamp),
            "type": v.subject.type,
            "service": v.subject.ref or v.subject.service or "unknown",
            "action": v.judgment.action,
            "summary": v.subject.summary,
        })
    events.sort(key=lambda e: e["timestamp"])
    return events


def _parse_ts(ts: Any) -> datetime:
    """Parse a timestamp that might be a string or datetime."""
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=UTC)
        return ts
    s = str(ts).replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _spec_files(specs_path: Path) -> list[Path]:
    """Every manifest-suffixed file directly under ``specs_path``, in a stable
    order.

    Both suffixes, because ``observe/slo/spec_loader.py`` has always accepted
    both and a ``.yml`` manifest that this glob could not see was dropped from
    the financial figure with ``parse_failures == 0`` — the same silent-subset
    failure as opensrm-oh27, reached by file extension rather than parse error.

    Sorted, because the duplicate-skip branch below is first-wins and would
    otherwise resolve on filesystem order.
    """
    return sorted(
        p for p in specs_path.iterdir() if p.suffix in (".yaml", ".yml")
    )


def _declares_itself_a_manifest(spec_file: Path) -> bool:
    """Whether a file that failed to load was *claiming* to be a manifest.

    ``load_manifest`` raises ``ManifestLoadError`` for any YAML it cannot parse
    as a manifest, including files that never claimed to be one — a
    ``kustomization.yaml``, a Prometheus rules file. Counting those would fire
    the caller's "computed over a subset" caveat on every run over a mixed
    directory, and a caveat that always fires stops being read.

    Uses the canonical v1/v2 format predicates rather than a local re-reading
    of apiVersion/kind, so this cannot drift from what the parser accepts.
    YAML too malformed to inspect counts as a manifest: a syntax error inside
    a specs directory is a deployment error either way.
    """
    try:
        data = yaml.safe_load(spec_file.read_text())
    except (OSError, yaml.YAMLError):
        return True
    if not isinstance(data, dict):
        return False
    # "service" is the legacy pre-apiVersion shape load_manifest still accepts.
    return is_opensrm_v2_format(data) or is_srm_v1_format(data) or "service" in data


@dataclass(frozen=True)
class LoadedManifests:
    """Outcome of scanning a specs directory (opensrm-oh27).

    ``manifests`` is the parsed view keyed by service name; ``parse_failures``
    counts *files* that raised ``ManifestLoadError``, not services missing
    from ``manifests`` — a failed environment overlay for a service whose
    base manifest parsed still counts one. The count is deliberately a
    coverage-doubt signal rather than a precise miss-count, and it travels
    with the manifests because the downstream financial figure is computed
    over ``manifests`` alone: without it, a partial view is
    indistinguishable from a complete one. The per-file
    ``manifest_parse_failed`` warning carries which file and why.
    """

    manifests: dict[str, Any]
    parse_failures: int = 0


def _load_manifests_from_specs(specs_dir: str | None) -> LoadedManifests:
    """Load all ``*.yaml`` manifests under ``specs_dir`` once, keyed by
    ``manifest.name`` (the canonical service identity).

    Returns empty manifests when ``specs_dir`` is None or not a directory,
    so callers can treat the result uniformly without re-checking the
    filesystem. Duplicate manifests for the same service (template forks,
    environment overlays) are deduplicated by keeping the first one
    encountered and emitting a warning — this matches the pre-jmy.21
    behaviour of ``_compute_financial_impact``.

    Parse failures from ``load_manifest`` are logged and counted, then
    skipped (opensrm-oh27). The financial-impact and dependency-extraction
    paths both tolerate a partial view, but the caller is told how partial
    it is via ``LoadedManifests.parse_failures``.
    """
    if not specs_dir:
        return LoadedManifests(manifests={})
    specs_path = Path(specs_dir)
    if not specs_path.is_dir():
        return LoadedManifests(manifests={})

    manifests: dict[str, Any] = {}
    parse_failures = 0
    for spec_file in _spec_files(specs_path):
        try:
            manifest = load_manifest(str(spec_file))
        except ManifestLoadError as exc:
            if not _declares_itself_a_manifest(spec_file):
                # Foreign YAML sharing the directory, not a broken manifest.
                log.debug("manifest_file_ignored", spec_file=str(spec_file))
                continue
            parse_failures += 1
            log.warning(
                "manifest_parse_failed",
                spec_file=str(spec_file),
                error=str(exc),
            )
            continue
        if manifest.name in manifests:
            log.warning(
                "manifest_duplicate_skipped",
                service=manifest.name,
                spec_file=str(spec_file),
            )
            continue
        manifests[manifest.name] = manifest
    return LoadedManifests(manifests=manifests, parse_failures=parse_failures)


def _extract_declared_dependencies(
    manifests_by_service: dict[str, Any],
) -> dict[str, list[str]]:
    """Wrapper around the shared helper (opensrm-dpws). The CLI path
    works with Manifest dataclass instances loaded via load_manifest().
    """
    return extract_declared_dependencies(from_manifests=manifests_by_service)


def _compute_financial_impact(
    blast_radius: list,
    duration_minutes: float,
    breach_counts_by_service: dict[str, int],
    specs_dir: str | None,
    *,
    manifests_by_service: dict[str, Any] | None = None,
) -> dict | None:
    """Compute spec § 1 financial impact across blast-radius services.

    For each service in ``blast_radius`` whose manifest carries an
    ``outcomes`` block, attribute that service's per-service breach
    count (metric path) or fall back to ``estimated_daily_decisions``
    prorated to incident duration (spec_estimate path). Sum across
    services that share a currency; mixed-currency manifests are
    skipped with a warning.

    ``manifests_by_service`` is the shared cache produced by
    ``_load_manifests_from_specs(...).manifests`` (opensrm-jmy.21). When
    omitted the function loads manifests itself — preserves the pre-jmy.21
    calling convention used by ``tests/learn/test_retrospective_financial.py``.
    That self-loading path discards the parse-failure count; only
    ``build_retrospective`` surfaces it (as
    ``metadata.custom["manifest_parse_failures"]``), and the per-file
    warning log fires either way.
    """
    if not specs_dir or not blast_radius:
        return None

    if duration_minutes <= 0 and not breach_counts_by_service:
        # No duration signal AND no breach-derived metric path — both
        # estimators return nothing; surface why instead of silently
        # dropping the financial_impact field.
        log.warning(
            "financial_impact_skipped_no_duration_or_metrics",
            specs_dir=specs_dir,
        )
        return None

    if manifests_by_service is None:
        manifests_by_service = _load_manifests_from_specs(specs_dir).manifests
    if not manifests_by_service:
        return None

    affected_services = {
        s.get("service", s) if isinstance(s, dict) else s
        for s in blast_radius
    }

    total_estimated = 0.0
    total_decisions = 0
    currency: str | None = None
    aggregate_source: str | None = None

    for service_name, manifest in manifests_by_service.items():
        if service_name not in affected_services:
            continue
        if manifest.outcomes is None:
            continue

        # Metric path: this service's own breach count. Falls back to
        # spec_estimate when the service produced no breach evaluations.
        service_breaches = breach_counts_by_service.get(service_name, 0)
        if service_breaches > 0:
            decisions = service_breaches
            this_source = "metric"
        else:
            estimate = estimate_decisions_in_window(
                manifest.outcomes,
                window=timedelta(minutes=duration_minutes),
            )
            if estimate is None:
                continue
            decisions = estimate
            this_source = "spec_estimate"

        impact = compute_financial_impact(
            manifest.outcomes,
            decisions_affected=decisions,
            failure_mode="false_negative",
            volume_source=this_source,
        )
        if impact is None:
            continue

        if currency is None:
            currency = impact.currency
            aggregate_source = this_source
        else:
            if currency != impact.currency:
                log.warning(
                    "financial_impact_currency_mismatch_skipped",
                    service=service_name,
                    aggregate_currency=currency,
                    service_currency=impact.currency,
                )
                continue
            if aggregate_source != this_source:
                # Mixed metric + spec_estimate paths in one aggregate
                # are legitimate; tag conservatively.
                aggregate_source = "spec_estimate"

        total_estimated += impact.estimated
        total_decisions += impact.decisions_affected

    # currency is None means no service contributed a figure; cost=0
    # declared in spec is a legitimate "no exposure" answer that should
    # still report (currency, decisions, estimated=0).
    if currency is None:
        return None

    return {
        "estimated": round(total_estimated, 2),
        "currency": currency,
        "decisions_affected": total_decisions,
        "failure_mode": "false_negative",
        "volume_source": aggregate_source,
    }


def _generate_recommendations(
    evaluation_verdicts: list[Verdict],
    correlation_verdicts: list[Verdict],
    incident_custom: dict,
) -> list[dict[str, str]]:
    """Generate actionable recommendations from the incident data."""
    recs = []

    # Check if judgment SLOs were breached
    for v in evaluation_verdicts:
        custom = getattr(v.metadata, "custom", {}) or {}
        if custom.get("slo_type") == "judgment" and custom.get("breach"):
            slo_name = custom.get("slo_name", "unknown")
            target = custom.get("target")
            current = custom.get("current_value")
            service = v.subject.ref or "unknown"
            # target/current stored in 0-100 percentage convention (opensrm-5fff.1).
            target_str = f"{target:.1f}%" if target else "target"
            current_str = f"{current:.1f}%" if current else "current"
            recs.append({
                "type": "slo_gate",
                "detail": (
                    f"Block {service} model deploys when {slo_name} exceeds "
                    f"{target_str} in canary window. This incident "
                    f"({slo_name} hit {current_str}) would have been caught pre-production."
                ),
                "spec_field": "spec.deployment.gates.error_budget",
            })
            break

    # Check blast radius size
    blast = incident_custom.get("blast_radius", [])
    if len(blast) > 3:
        services_str = ", ".join(str(s) for s in blast[:5])
        recs.append({
            "type": "dependency_review",
            "detail": (
                f"Blast radius of {len(blast)} services ({services_str}) suggests tight coupling. "
                f"Add circuit breakers or bulkheads between critical dependency paths."
            ),
            "spec_field": "spec.dependencies",
        })

    # Check if root cause was a change
    root_causes = incident_custom.get("root_causes", [])
    for rc in root_causes:
        if isinstance(rc, dict) and rc.get("type") in ("model_deploy", "deploy", "config_change", "model_regression"):
            service = rc.get("service", "unknown")
            change_type = rc.get("type", "change")
            detail_text = rc.get("detail", "")
            recs.append({
                "type": "change_control",
                "detail": (
                    f"Require canary evaluation window before {change_type} on {service}. "
                    f"{detail_text}" if detail_text else
                    f"Require canary evaluation window before {change_type} on {service}. "
                    f"A staged rollout with automated quality gates would have limited blast radius."
                ),
                "spec_field": "spec.deployment.strategy",
            })
            break

    return recs


def _write_evaluation_record(
    decision_store: Any,
    *,
    incident_custom: dict,
    duration_minutes: float,
    decisions_affected: int,
    root_cause: dict | None,
    verdict_count: int,
    retro_timestamp: Any,
) -> None:
    """Write a content-addressed Evaluation record to the decision store."""
    import logging

    try:
        from nthlayer_common.records.hashing import canonical_json, compute_hash
        from nthlayer_common.records.models import (
            ZERO_HASH,
            Evaluation,
            EvaluationMethod,
            EvaluationOutcome,
            Summaries,
        )

        incident_id = incident_custom.get("incident_id")
        if not incident_id:
            logging.getLogger(__name__).warning("Skipping evaluation record: no incident_id in incident metadata")
            return
        timestamp = _parse_ts(retro_timestamp)

        # Determine method and outcome from the retrospective data
        method = EvaluationMethod.METRIC_RECOVERY
        if decisions_affected > 0:
            outcome = EvaluationOutcome.EFFECTIVE if duration_minutes < 60 else EvaluationOutcome.PARTIAL
        else:
            outcome = EvaluationOutcome.INCONCLUSIVE

        service = root_cause.get("service", "unknown") if root_cause else "unknown"
        technical = f"Retrospective: {service}, {duration_minutes:.0f}m duration, {decisions_affected} decisions affected, {verdict_count} verdicts"
        plain = f"Incident retrospective for {service} — {'resolved' if outcome == EvaluationOutcome.EFFECTIVE else 'partially resolved' if outcome == EvaluationOutcome.PARTIAL else 'inconclusive'}"

        summaries = Summaries(
            technical=technical[:280],
            plain=plain[:280],
        )

        chain = decision_store.get_chain("evaluation", incident_id)
        previous_hash = chain[-1].hash if chain else ZERO_HASH

        placeholder = Evaluation(
            hash="placeholder",
            previous_hash=previous_hash,
            schema_version="evaluation/v1",
            timestamp=timestamp,
            incident_id=incident_id,
            verdict_hash=ZERO_HASH,  # TODO: link to specific remediation verdict hash in future bead
            method=method,
            outcome=outcome,
            evidence_hashes=[],  # TODO: link to post-action assessment hashes in future bead
            payload={
                "duration_minutes": round(duration_minutes, 1),
                "decisions_affected": decisions_affected,
                "verdict_count": verdict_count,
                "root_cause": root_cause,
            },
            summaries=summaries,
        )
        canonical = canonical_json(placeholder)
        record_hash = compute_hash(canonical)

        record = Evaluation(
            hash=record_hash,
            previous_hash=previous_hash,
            schema_version="evaluation/v1",
            timestamp=timestamp,
            incident_id=incident_id,
            verdict_hash=ZERO_HASH,
            method=method,
            outcome=outcome,
            evidence_hashes=[],
            payload=placeholder.payload,
            summaries=summaries,
        )
        decision_store.put_evaluation(record)
    except Exception as exc:
        logging.getLogger(__name__).warning("Evaluation record write failed: %s", exc)
