"""Escalation state machine — tracks notification steps for an incident.

Pure data + methods. No I/O, no async. The EscalationRunner (runner.py)
drives this state machine and dispatches to notification backends.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nthlayer_workers.respond.notification_backends.protocol import NotificationResult


class EscalationStatus(Enum):
    """Lifecycle of an escalation."""

    ACTIVE = "active"
    ACKNOWLEDGED = "acknowledged"
    EXHAUSTED = "exhausted"
    RESOLVED = "resolved"


@dataclass
class EscalationStep:
    """A single step in the escalation policy."""

    after: timedelta  # Delay from escalation start
    notify: str  # "slack_dm" | "ntfy" | "slack_channel" | "phone" | "pagerduty"
    target: str | None = None  # "next_oncall" | "engineering_manager" | None (= current on-call)
    phone: str | None = None  # Direct phone override for this step


@dataclass
class EscalationState:
    """Tracks the state of an active escalation for one incident.

    The runner calls ``next_due_step(now)`` in a loop. Each call either
    returns the next step to execute or None (not yet due / finished).
    Calling ``acknowledge()`` or ``resolve()`` stops all further steps.
    """

    incident_id: str
    started_at: datetime
    steps: list[EscalationStep]

    # Mutable state
    current_step_index: int = 0
    acknowledged_by: str | None = None
    acknowledged_at: datetime | None = None
    status: EscalationStatus = EscalationStatus.ACTIVE
    notifications_sent: list[NotificationResult] = field(default_factory=list)

    def acknowledge(self, user: str, at: datetime) -> None:
        """Mark escalation as acknowledged. Stops all further steps."""
        self.acknowledged_by = user
        self.acknowledged_at = at
        self.status = EscalationStatus.ACKNOWLEDGED

    def resolve(self) -> None:
        """Mark escalation as resolved."""
        self.status = EscalationStatus.RESOLVED

    def next_due_step(self, now: datetime) -> EscalationStep | None:
        """Return the next escalation step that should fire, or None.

        Steps fire when:
        - Status is ACTIVE (not acked/resolved/exhausted)
        - Current time >= started_at + step.after
        - The step hasn't been executed yet (tracked by current_step_index)
        """
        if self.status != EscalationStatus.ACTIVE:
            return None

        if self.current_step_index >= len(self.steps):
            self.status = EscalationStatus.EXHAUSTED
            return None

        step = self.steps[self.current_step_index]
        due_at = self.started_at + step.after

        if now >= due_at:
            self.current_step_index += 1
            return step

        return None

    def time_until_next_step(self, now: datetime) -> timedelta | None:
        """How long until the next step fires. For the polling loop."""
        if self.status != EscalationStatus.ACTIVE:
            return None
        if self.current_step_index >= len(self.steps):
            return None

        step = self.steps[self.current_step_index]
        due_at = self.started_at + step.after
        remaining = due_at - now
        return max(remaining, timedelta(0))
