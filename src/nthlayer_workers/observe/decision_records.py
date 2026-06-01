"""Bridge between observe's legacy assessments and content-addressed decision records.

Converts nthlayer-observe Assessment objects into content-addressed Assessment records
with hash chains, severity mapping, stream identifiers, and template-based summaries.
"""

from __future__ import annotations

from nthlayer_common.records.hashing import canonical_json, compute_hash
from nthlayer_common.records.models import (
    Assessment as DecisionAssessment,
)
from nthlayer_common.records.models import (
    AssessmentType,
    Severity,
    Summaries,
)

from nthlayer_workers.observe.assessment import Assessment as LegacyAssessment

__all__ = [
    "build_decision_record",
    "build_stream",
    "generate_summaries",
    "map_severity",
]

# --- Assessment type mapping ---

_TYPE_MAP: dict[str, AssessmentType] = {
    "slo_status": AssessmentType.THRESHOLD_BREACH,
    "drift_signal": AssessmentType.DRIFT,
    "verification": AssessmentType.CHANGE_EVENT,
    "deploy_gate": AssessmentType.CHANGE_EVENT,
    "dependency_graph": AssessmentType.CHANGE_EVENT,
    "portfolio_status": AssessmentType.CHANGE_EVENT,
}

# --- Severity mapping ---

_SLO_STATUS_SEVERITY: dict[str, Severity] = {
    "EXHAUSTED": Severity.CRITICAL,
    "CRITICAL": Severity.CRITICAL,
    "WARNING": Severity.WARNING,
    "HEALTHY": Severity.INFO,
    "NO_DATA": Severity.WARNING,
    "ERROR": Severity.WARNING,
}

_DRIFT_SEVERITY: dict[str, Severity] = {
    "critical": Severity.CRITICAL,
    "warn": Severity.WARNING,
    "info": Severity.INFO,
    "none": Severity.INFO,
}

_GATE_SEVERITY: dict[str, Severity] = {
    "blocked": Severity.CRITICAL,
    "warning": Severity.WARNING,
    "approved": Severity.INFO,
}


def map_severity(assessment_type: str, data: dict) -> Severity:
    """Map assessment data to decision record severity."""
    if assessment_type == "slo_status":
        return _SLO_STATUS_SEVERITY.get(data.get("status", ""), Severity.WARNING)
    if assessment_type == "drift_signal":
        return _DRIFT_SEVERITY.get(data.get("severity", ""), Severity.INFO)
    if assessment_type == "verification":
        exit_code = data.get("exit_code", 0)
        if exit_code >= 2:
            return Severity.CRITICAL
        if exit_code >= 1:
            return Severity.WARNING
        return Severity.INFO
    if assessment_type == "deploy_gate":
        return _GATE_SEVERITY.get(data.get("decision", ""), Severity.WARNING)
    if assessment_type == "dependency_graph":
        return Severity.WARNING if data.get("errors") else Severity.INFO
    return Severity.INFO


# --- Stream construction ---


def build_stream(legacy: LegacyAssessment) -> str:
    """Build a colon-delimited stream identifier from a legacy assessment."""
    if legacy.kind in ("slo_status", "drift_signal"):
        slo_name = legacy.data.get("slo_name", "unknown")
        return f"sli:{legacy.service}:{slo_name}"
    return f"{legacy.kind}:{legacy.service}"


# --- Summary generation ---


def generate_summaries(legacy: LegacyAssessment) -> Summaries:
    """Generate template-based summaries from a legacy assessment."""
    d = legacy.data
    svc = legacy.service

    if legacy.kind == "slo_status":
        slo = d.get("slo_name", "unknown")
        status = d.get("status", "UNKNOWN")
        pct = d.get("percent_consumed")
        sli = d.get("current_sli")
        obj = d.get("objective")
        technical = f"{svc} {slo}: {status}, {pct}% budget consumed. SLI={sli}, target={obj}."
        plain = f"{svc} {slo} is {'breaching' if status in ('CRITICAL', 'EXHAUSTED') else 'within'} its error budget ({pct}% used)."
        executive = f"{svc} SLO {status.lower()} — {pct}% budget consumed."

    elif legacy.kind == "drift_signal":
        slo = d.get("slo_name", "unknown")
        pattern = d.get("pattern", "unknown")
        slope = d.get("slope_per_week", 0)
        days = d.get("days_until_exhaustion")
        technical = f"{svc} {slo}: {pattern} drift at {slope}%/week."
        if days:
            technical += f" Exhaustion in {days}d."
        plain = d.get("summary", f"{svc} {slo} budget is drifting.")
        executive = f"{svc} SLO drift — {'action needed' if d.get('severity') in ('warn', 'critical') else 'stable'}."

    elif legacy.kind == "verification":
        found = d.get("found_metrics", 0)
        declared = d.get("declared_metrics", 0)
        missing_crit = d.get("missing_critical", [])
        technical = f"{svc}: {found}/{declared} metrics verified."
        if missing_crit:
            technical += f" Missing critical: {', '.join(missing_crit[:3])}."
        plain = f"{svc} metric verification: {'all present' if not missing_crit else f'{len(missing_crit)} critical missing'}."
        executive = f"{svc} metrics {'verified' if not missing_crit else 'incomplete'}."

    elif legacy.kind == "deploy_gate":
        decision = d.get("decision", "unknown").upper()
        budget = d.get("budget_remaining_pct", 0)
        reasons = d.get("reasons", [])
        technical = f"{svc} deploy gate: {decision}. Budget remaining: {budget}%."
        if reasons:
            technical += f" {reasons[0]}"
        plain = f"{svc} deployment {'allowed' if decision == 'APPROVED' else 'needs review' if decision == 'WARNING' else 'blocked'}."
        executive = f"{svc} deploy {decision.lower()}."

    elif legacy.kind == "dependency_graph":
        count = d.get("dependencies_discovered", 0)
        upstream = d.get("upstream", [])
        downstream = d.get("downstream", [])
        errors = d.get("errors", [])
        technical = f"{svc}: {count} dependencies ({len(upstream)} upstream, {len(downstream)} downstream)."
        if errors:
            technical += f" {len(errors)} provider errors."
        plain = f"{svc} has {count} discovered dependencies."
        executive = f"{svc}: {count} deps{'.' if not errors else f', {len(errors)} errors.'}"

    elif legacy.kind == "portfolio_status":
        total = d.get("total_services", 0)
        healthy = d.get("healthy_count", 0)
        warning = d.get("warning_count", 0)
        critical = d.get("critical_count", 0)
        exhausted = d.get("exhausted_count", 0)
        technical = f"Portfolio: {total} services — {healthy} healthy, {warning} warning, {critical} critical, {exhausted} exhausted."
        plain = f"Portfolio health: {healthy}/{total} services healthy."
        executive = f"Portfolio: {healthy}/{total} healthy."

    else:
        technical = f"{svc}: {legacy.kind} assessment."
        plain = technical
        executive = technical

    return Summaries(
        technical=technical[:280],
        plain=plain[:280],
        executive=executive[:140],
    )


# --- Full record building ---


def build_decision_record(
    legacy: LegacyAssessment,
    *,
    previous_hash: str,
    incident_id: str | None = None,
) -> DecisionAssessment:
    """Convert a legacy observe Assessment into a content-addressed decision record.

    Computes the SHA-256 hash from the canonical form. The caller is responsible
    for providing the correct ``previous_hash`` (chain tail for this stream).
    """
    assessment_type = _TYPE_MAP.get(legacy.kind, AssessmentType.CHANGE_EVENT)
    severity = map_severity(legacy.kind, legacy.data)
    stream = build_stream(legacy)
    summaries = generate_summaries(legacy)

    # Build record without hash to compute canonical form
    placeholder = DecisionAssessment(
        hash="placeholder",
        previous_hash=previous_hash,
        schema_version="assessment/v1",
        timestamp=legacy.created_at,
        stream=stream,
        incident_id=incident_id,
        type=assessment_type,
        severity=severity,
        payload=legacy.data,
        summaries=summaries,
    )
    canonical = canonical_json(placeholder)
    record_hash = compute_hash(canonical)

    return DecisionAssessment(
        hash=record_hash,
        previous_hash=previous_hash,
        schema_version="assessment/v1",
        timestamp=legacy.created_at,
        stream=stream,
        incident_id=incident_id,
        type=assessment_type,
        severity=severity,
        payload=legacy.data,
        summaries=summaries,
    )
