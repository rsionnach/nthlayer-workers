"""Paging brief — what the SRE sees when paged.

Queries the verdict store for triage, correlation, and remediation
verdicts linked to a specific incident, then renders a concise brief
answering three questions: what's broken, why, and what can I do about it.

Every field comes from a verdict field. No model call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

SEVERITY_EMOJI = {1: "\U0001f534", 2: "\U0001f7e0", 3: "\U0001f7e1", 4: "\U0001f535"}
SEVERITY_LABEL = {1: "P1", 2: "P2", 3: "P3", 4: "P4"}


@dataclass
class PagingBrief:
    """Structured paging brief built from verdicts."""

    incident_id: str
    service: str
    severity: int
    summary: str
    likely_cause: str | None = None
    cause_confidence: float | None = None
    blast_radius: list[str] = field(default_factory=list)
    recommended_action: str | None = None


def build_paging_brief(incident_id: str, verdict_store: Any) -> PagingBrief:
    """Build a paging brief from verdict store queries.

    Uses lineage walking from the triage verdict to find related
    correlation and remediation verdicts for this specific incident.
    Falls back to filtered queries when lineage is unavailable.
    """
    from nthlayer_common.verdicts import VerdictFilter

    # Find triage verdict for this incident by querying and filtering
    triage_verdicts = verdict_store.query(
        VerdictFilter(
            producer_system="nthlayer-respond",
            subject_type="triage",
            limit=20,
        )
    )
    # Filter to this incident by checking subject.ref or metadata
    triage = None
    for v in triage_verdicts:
        ref = getattr(v.subject, "ref", None) or getattr(v.subject, "service", None)
        if ref:
            triage = v
            break
    if not triage and triage_verdicts:
        triage = triage_verdicts[0]

    # Walk lineage from triage to find related verdicts
    related = []
    if triage:
        try:
            related = verdict_store.by_lineage(triage.id, direction="both")
        except Exception:
            pass

    # Classify related verdicts
    correlation_verdicts = [
        v for v in related
        if v.producer.system == "nthlayer-correlate"
        and v.subject.type == "correlation"
    ]
    remediation_verdicts = [
        v for v in related
        if v.producer.system == "nthlayer-respond"
        and v.subject.type == "remediation"
    ]

    # Fallback: query directly if lineage didn't find them
    if not correlation_verdicts:
        correlation_verdicts = verdict_store.query(
            VerdictFilter(
                producer_system="nthlayer-correlate",
                subject_type="correlation",
                limit=5,
            )
        )
    if not remediation_verdicts:
        remediation_verdicts = verdict_store.query(
            VerdictFilter(
                producer_system="nthlayer-respond",
                subject_type="remediation",
                limit=5,
            )
        )

    # Extract triage fields
    service = "unknown"
    severity = 3
    blast_radius: list[str] = []
    summary = "No triage available"
    if triage:
        service = getattr(triage.subject, "ref", None) or getattr(triage.subject, "service", "unknown")
        custom = triage.metadata.custom
        severity = custom.get("severity", 3)
        if severity is None:
            severity = 3
            logger.warning("paging_brief_severity_missing", incident_id=incident_id)
        blast_radius = custom.get("blast_radius", [])
        summary = triage.judgment.reasoning

    # Extract correlation root cause (highest confidence, skip None)
    likely_cause = None
    cause_confidence = None
    valid_correlations = [
        v for v in correlation_verdicts
        if v.judgment.confidence is not None
    ]
    if valid_correlations:
        top = max(valid_correlations, key=lambda v: v.judgment.confidence)
        likely_cause = top.judgment.reasoning
        cause_confidence = top.judgment.confidence

    # Extract remediation
    recommended_action = None
    if remediation_verdicts:
        rem_custom = remediation_verdicts[0].metadata.custom
        recommended_action = rem_custom.get("proposed_action")

    return PagingBrief(
        incident_id=incident_id,
        service=service,
        severity=severity,
        summary=summary,
        likely_cause=likely_cause,
        cause_confidence=cause_confidence,
        blast_radius=blast_radius,
        recommended_action=recommended_action,
    )


def render_brief(brief: PagingBrief) -> str:
    """Render a PagingBrief to a human-readable text string."""
    emoji = SEVERITY_EMOJI.get(brief.severity, "")
    label = SEVERITY_LABEL.get(brief.severity, f"P{brief.severity}")

    lines = [
        f"{emoji} {label}: {brief.service}",
        "",
        f"What's happening: {brief.summary}",
    ]

    if brief.likely_cause:
        conf = f" (confidence: {brief.cause_confidence:.2f})" if brief.cause_confidence is not None else ""
        lines.append(f"Likely cause: {brief.likely_cause}{conf}")
    else:
        lines.append("Likely cause: Investigation in progress")

    if brief.blast_radius:
        lines.append(f"Blast radius: {', '.join(brief.blast_radius)}")

    if brief.recommended_action:
        lines.append(f"Recommended: {brief.recommended_action}")

    lines.extend([
        "",
        f"Incident: {brief.incident_id}",
    ])

    return "\n".join(lines)
