"""Notification backend protocol and shared dataclasses.

Every notification channel (Slack, ntfy, Twilio, PagerDuty, stdout)
implements the ``NotificationBackend`` protocol. The escalation engine
dispatches to backends without knowing delivery details.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nthlayer_workers.respond.oncall.schedule import RosterMember


@dataclass
class NotificationPayload:
    """What to tell the human."""

    incident_id: str
    severity: int  # 1 = P1 (critical), 2 = P2 (major), 3 = P3 (minor), 4 = P4 (info)
    title: str
    summary: str
    root_cause: str | None
    blast_radius: list[str]
    actions_url: str | None
    escalation_step: int
    requires_ack: bool


@dataclass
class NotificationResult:
    """What happened when we tried to notify."""

    delivered: bool
    channel: str  # "slack_dm" | "ntfy" | "phone" | "pagerduty" | "stdout"
    recipient: str
    timestamp: datetime
    message_id: str | None
    error: str | None


@runtime_checkable
class NotificationBackend(Protocol):
    """Protocol for all notification delivery mechanisms.

    Each backend handles one delivery channel. Adding a new channel
    is one file implementing this protocol.
    """

    async def send(
        self,
        recipient: RosterMember,
        payload: NotificationPayload,
    ) -> NotificationResult: ...

    async def health_check(self) -> bool: ...
