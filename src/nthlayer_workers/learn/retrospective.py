"""Retrospective builder — walks verdict lineage to produce a post-incident analysis."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from nthlayer_learn.core import create, link
from nthlayer_learn.models import Verdict
from nthlayer_learn.store import VerdictStore, VerdictFilter


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

    # Compute decisions affected (count evaluation verdicts with breach=True)
    decisions_affected = sum(
        1 for v in evaluation_verdicts
        if (getattr(v.metadata, "custom", {}) or {}).get("breach")
    )

    # Build blast radius from correlation and incident metadata
    blast_radius = []
    for v in correlation_verdicts:
        corr_custom = getattr(v.metadata, "custom", {}) or {}
        blast_radius.extend(corr_custom.get("blast_radius", []))
    if not blast_radius:
        blast_radius = incident_custom.get("blast_radius", [])

    # Financial impact (if specs available)
    financial_impact = _compute_financial_impact(
        blast_radius, duration_minutes, specs_dir
    )

    # Generate recommendations
    recommendations = _generate_recommendations(
        evaluation_verdicts, correlation_verdicts, incident_custom
    )

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
        metadata={"custom": {
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
        }},
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
            return ts.replace(tzinfo=timezone.utc)
        return ts
    s = str(ts).replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _compute_financial_impact(
    blast_radius: list,
    duration_minutes: float,
    specs_dir: str | None,
) -> dict | None:
    """Compute estimated financial impact from spec outcomes blocks."""
    if not specs_dir or not blast_radius:
        return None

    from pathlib import Path
    import yaml

    specs_path = Path(specs_dir)
    if not specs_path.is_dir():
        return None

    total_cost = 0.0
    for spec_file in specs_path.glob("*.yaml"):
        try:
            raw = yaml.safe_load(spec_file.read_text())
        except Exception:
            continue
        if not isinstance(raw, dict):
            continue

        service = raw.get("metadata", {}).get("name", "")
        affected_services = [
            s.get("service", s) if isinstance(s, dict) else s
            for s in blast_radius
        ]
        if service not in affected_services:
            continue

        outcomes = raw.get("spec", {}).get("outcomes", {})
        if not outcomes:
            continue

        # Simple model: revenue_per_minute * duration * error_rate
        rpm = outcomes.get("revenue_per_minute", 0)
        total_cost += rpm * duration_minutes

    if total_cost <= 0:
        return None

    return {
        "estimated": round(total_cost, 2),
        "currency": "USD",
        "failure_mode": "service_degradation",
        "volume_source": "spec.outcomes.revenue_per_minute",
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
            target_str = f"{target * 100:.1f}%" if target else "target"
            current_str = f"{current * 100:.1f}%" if current else "current"
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
