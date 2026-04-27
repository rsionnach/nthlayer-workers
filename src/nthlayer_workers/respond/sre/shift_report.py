"""Shift report — summary of what happened during an on-call shift.

At shift start, the SRE receives a summary of incidents, governance
changes, evaluations, and pending reviews from the verdict store.
Every field comes from verdict queries. No model call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class ShiftReport:
    """Structured shift report built from verdict queries."""

    window_start: datetime
    window_end: datetime
    incidents: list[Any] = field(default_factory=list)
    evaluations_count: int = 0
    pending_reviews: int = 0


def build_shift_report(
    shift_start: datetime,
    shift_end: datetime,
    verdict_store: Any,
) -> ShiftReport:
    """Build a shift report from verdict store queries.

    Queries for incidents (triage + custom incident verdicts),
    evaluations, and pending reviews within the shift window.
    """
    from nthlayer_common.verdicts import VerdictFilter

    # Triage verdicts in window (real incidents)
    triage_verdicts = verdict_store.query(
        VerdictFilter(
            producer_system="nthlayer-respond",
            subject_type="triage",
            from_time=shift_start,
            to_time=shift_end,
            limit=0,
        )
    )

    # Custom verdicts with incident_type (incident summaries)
    custom_verdicts = verdict_store.query(
        VerdictFilter(
            subject_type="custom",
            from_time=shift_start,
            to_time=shift_end,
            limit=0,
        )
    )
    incident_summaries = [
        v for v in custom_verdicts
        if v.metadata.custom.get("incident_type") == "incident"
    ]

    # Combine — triage verdicts are incidents, plus explicit incident summaries
    incidents = triage_verdicts + incident_summaries

    # Evaluations in window
    evaluations = verdict_store.query(
        VerdictFilter(
            producer_system="nthlayer-measure",
            subject_type="evaluation",
            from_time=shift_start,
            to_time=shift_end,
            limit=100,
        )
    )

    # Pending reviews (no time filter — all currently pending)
    pending = verdict_store.query(
        VerdictFilter(
            status="pending",
            limit=100,
        )
    )

    return ShiftReport(
        window_start=shift_start,
        window_end=shift_end,
        incidents=incidents,
        evaluations_count=len(evaluations),
        pending_reviews=len(pending),
    )


def is_quiet(report: ShiftReport) -> bool:
    """Determine if a shift was quiet — no incidents and no pending reviews."""
    return len(report.incidents) == 0 and report.pending_reviews == 0


def render_shift_report(report: ShiftReport) -> str:
    """Render a ShiftReport to human-readable text."""
    start = report.window_start.strftime("%b %d, %H:%M")
    end = report.window_end.strftime("%b %d, %H:%M")

    lines = [f"Shift Report: {start} \u2192 {end}", ""]

    if is_quiet(report):
        lines.append("Nothing required your attention. \u2713")
    else:
        count = len(report.incidents)
        if count > 0:
            lines.append(f"{count} incident{'s' if count != 1 else ''} during this shift.")
            for inc in report.incidents:
                custom = inc.metadata.custom
                sev = custom.get("severity", "?")
                dur = custom.get("duration", "")
                dur_str = f" ({dur})" if dur else ""
                lines.append(f"  P{sev}: {inc.subject.summary}{dur_str}")

    if report.evaluations_count > 0:
        lines.append(f"\nEvaluations: {report.evaluations_count}")

    if report.pending_reviews > 0:
        lines.append(f"Pending reviews: {report.pending_reviews}")

    return "\n".join(lines)
