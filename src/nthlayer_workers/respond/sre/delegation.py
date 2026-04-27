"""Delegation mode — 'I'm busy, handle it'.

During a multi-incident situation, the SRE delegates a lower-priority
incident to autonomous handling. The coordinator continues with
pre-approved safe actions only. Notifications are suppressed except
for resolution or escalation.

No model call. Delegation is a governance configuration change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

import structlog

logger = structlog.get_logger(__name__)

_DEFAULT_MAX_DURATION = timedelta(hours=2)

# Only these event types break through delegation silence
_NOTIFY_EVENTS = frozenset({"resolution", "escalation"})


class DelegationStatus(Enum):
    """Lifecycle of a delegation."""

    ACTIVE = "active"
    EXPIRED = "expired"
    ESCALATED = "escalated"
    RESOLVED = "resolved"


@dataclass
class Delegation:
    """Tracks delegation state for an incident."""

    incident_id: str
    delegated_by: str
    delegated_at: datetime
    expires_at: datetime
    max_duration: timedelta
    safe_actions_only: bool = True
    status: DelegationStatus = DelegationStatus.ACTIVE


def create_delegation(
    *,
    incident_id: str,
    delegated_by: str,
    safe_actions_only: bool = True,
    max_duration: timedelta = _DEFAULT_MAX_DURATION,
) -> Delegation:
    """Create a delegation for an incident.

    The SRE won't receive updates until resolution or escalation.
    If safe actions are insufficient, the delegation escalates back.
    Auto-expires after ``max_duration``.
    """
    now = datetime.now(timezone.utc)

    logger.info(
        "delegation_created",
        incident_id=incident_id,
        delegated_by=delegated_by,
        safe_actions_only=safe_actions_only,
        max_duration_hours=max_duration.total_seconds() / 3600,
    )

    return Delegation(
        incident_id=incident_id,
        delegated_by=delegated_by,
        delegated_at=now,
        expires_at=now + max_duration,
        max_duration=max_duration,
        safe_actions_only=safe_actions_only,
    )


def check_delegation_expired(delegation: Delegation, now: datetime) -> bool:
    """Check if a delegation has expired.

    Already-resolved or escalated delegations are not considered expired.
    """
    if delegation.status != DelegationStatus.ACTIVE:
        return False
    return now >= delegation.expires_at


def should_notify_delegator(delegation: Delegation, event_type: str) -> bool:
    """Determine if the delegator should be notified for this event.

    Only resolution and escalation events break through the delegation
    silence while the delegation is active. Inactive delegations
    (resolved, expired, escalated) do not filter notifications.
    """
    if delegation.status != DelegationStatus.ACTIVE:
        return True  # No longer delegated — all events pass through
    return event_type in _NOTIFY_EVENTS
