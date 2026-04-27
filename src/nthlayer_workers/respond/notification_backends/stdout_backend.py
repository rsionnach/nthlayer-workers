"""Stdout notification backend — for testing and local development."""

from __future__ import annotations

from datetime import datetime, timezone

import structlog

from nthlayer_workers.respond.notification_backends.protocol import (
    NotificationPayload,
    NotificationResult,
)
from nthlayer_workers.respond.oncall.schedule import RosterMember

logger = structlog.get_logger(__name__)

SEVERITY_LABEL = {1: "P1 CRITICAL", 2: "P2 MAJOR", 3: "P3 MINOR", 4: "P4 INFO"}


class StdoutNotificationBackend:
    """Print notifications to stdout. For testing and local development."""

    async def send(
        self, recipient: RosterMember, payload: NotificationPayload
    ) -> NotificationResult:
        label = SEVERITY_LABEL.get(payload.severity, f"P{payload.severity}")
        lines = [
            f"{'=' * 60}",
            f"NOTIFICATION -> {recipient.name}",
            f"  Incident: {payload.incident_id}",
            f"  Severity: {label}",
            f"  Title: {payload.title}",
            f"  Summary: {payload.summary}",
        ]
        if payload.root_cause:
            lines.append(f"  Root cause: {payload.root_cause}")
        if payload.blast_radius:
            lines.append(f"  Blast radius: {', '.join(payload.blast_radius)}")
        lines.append(f"  Escalation step: {payload.escalation_step}")
        if payload.requires_ack:
            lines.append("  Requires acknowledgment: yes")
        lines.append(f"{'=' * 60}")

        print("\n".join(lines))  # noqa: T201 — CLI entrypoint output

        return NotificationResult(
            delivered=True,
            channel="stdout",
            recipient=recipient.name,
            timestamp=datetime.now(timezone.utc),
            message_id=None,
            error=None,
        )

    async def health_check(self) -> bool:
        return True
