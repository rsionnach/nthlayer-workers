"""Post-incident review — auto-generated from the verdict chain.

Reconstructs the incident timeline, classifies what worked vs what
needs improvement, and shows per-verdict accuracy. Every section
is built from verdict queries. The optional model call for action
item suggestions is clearly bounded and off by default.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# Agent systems whose verdicts are included in accuracy tracking
_AGENT_SYSTEMS = {"nthlayer-respond", "nthlayer-correlate", "nthlayer-measure"}


@dataclass
class TimelineEntry:
    """A single event in the incident timeline."""

    timestamp: datetime
    source: str
    agent: str
    summary: str
    confidence: float | None = None


@dataclass
class PostIncidentReview:
    """Structured post-incident review."""

    incident_id: str
    timeline: list[TimelineEntry] = field(default_factory=list)
    worked: list[str] = field(default_factory=list)
    improve: list[str] = field(default_factory=list)
    accuracy: list[str] = field(default_factory=list)


def build_timeline(verdicts: list[Any]) -> list[TimelineEntry]:
    """Reconstruct the incident timeline from verdict timestamps.

    Pure query + sort. No model call.
    """
    entries = []
    for v in verdicts:
        entries.append(
            TimelineEntry(
                timestamp=v.timestamp,
                source=v.producer.system,
                agent=v.producer.instance,
                summary=v.subject.summary,
                confidence=v.judgment.confidence,
            )
        )
    entries.sort(key=lambda e: e.timestamp)
    return entries


def build_review_sections(verdicts: list[Any]) -> tuple[list[str], list[str]]:
    """Build 'what worked' and 'what to improve' from verdict outcomes.

    Confirmed verdicts → worked. Overridden verdicts → improve.
    Pending/expired verdicts are excluded.
    """
    worked = []
    improve = []

    for v in verdicts:
        status = v.outcome.status
        instance = v.producer.instance

        if status == "confirmed":
            worked.append(f"{instance}: {v.subject.summary} (confirmed \u2713)")
        elif status == "overridden":
            reason = ""
            if v.outcome.override and v.outcome.override.reasoning:
                reason = f" — {v.outcome.override.reasoning}"
            improve.append(f"{instance}: overridden{reason}")

    return worked, improve


def build_accuracy_section(verdicts: list[Any]) -> list[str]:
    """Show confirmed/overridden status for each agent verdict.

    Only includes verdicts from agent systems (nthlayer-respond,
    nthlayer-correlate, nthlayer-measure). Human/manual verdicts excluded.
    """
    lines = []
    for v in verdicts:
        if v.producer.system not in _AGENT_SYSTEMS:
            continue

        status = v.outcome.status
        instance = v.producer.instance

        if status == "confirmed":
            lines.append(f"  {instance}: confirmed \u2713")
        elif status == "overridden":
            reason = ""
            if v.outcome.override and v.outcome.override.reasoning:
                reason = f" ({v.outcome.override.reasoning})"
            lines.append(f"  {instance}: overridden \u2717{reason}")

    return lines


def render_post_incident(review: PostIncidentReview) -> str:
    """Render a PostIncidentReview to human-readable text."""
    lines = [
        f"Post-Incident Review: {review.incident_id}",
        "Auto-generated from verdict chain.",
        "",
    ]

    if review.timeline:
        lines.append("Timeline:")
        for entry in review.timeline:
            ts = entry.timestamp.strftime("%H:%M")
            conf = f" ({entry.confidence:.0%})" if entry.confidence is not None else ""
            lines.append(f"  {ts}  {entry.source}: {entry.summary}{conf}")
        lines.append("")

    if review.worked:
        lines.append("What worked:")
        for item in review.worked:
            lines.append(f"  \u2022 {item}")
        lines.append("")

    if review.improve:
        lines.append("What to improve:")
        for item in review.improve:
            lines.append(f"  \u2022 {item}")
        lines.append("")

    if review.accuracy:
        lines.append("Verdict accuracy:")
        lines.extend(review.accuracy)

    return "\n".join(lines)
