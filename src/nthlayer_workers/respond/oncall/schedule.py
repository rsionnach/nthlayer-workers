"""On-call schedule resolver — pure function, no state, no database.

Given the oncall config and a timestamp, returns who is on call.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class RosterMember:
    """A person in the on-call rotation.

    Intentionally separate from nthlayer.specs.manifest.RosterMember —
    that is the build-time schema model parsed from YAML; this is the
    runtime model used by the schedule resolver and escalation engine.
    Extraction to nthlayer-common is planned when the API stabilises.
    """

    name: str
    slack_id: str
    ntfy_topic: str | None = None
    phone: str | None = None


@dataclass
class OnCallResult:
    """Who is on call right now."""

    primary: RosterMember
    secondary: RosterMember  # Next in rotation after primary. Equals primary for single-person rosters.
    rotation_handoff: datetime  # When primary's shift ends (next handoff for rotation, override end for overrides).
    source: Literal["rotation", "override"]


# Lookup dict for day names — no regex (per project convention).
DAY_MAP = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def resolve_oncall(oncall_config: dict, now: datetime) -> OnCallResult:
    """Determine who is on call at the given time.

    Algorithm:
    1. Check overrides first — if an override covers ``now``, that person is primary.
    2. Otherwise compute rotation position:
       - Parse handoff time and timezone
       - Calculate elapsed time since anchor handoff
       - Divide by rotation period (1 week for weekly, 1 day for daily)
       - Modulo roster length = current position
    3. Secondary is always the next person in rotation after primary.
    """
    roster = [
        RosterMember(
            name=r["name"],
            slack_id=r["slack_id"],
            ntfy_topic=r.get("ntfy_topic"),
            phone=r.get("phone"),
        )
        for r in oncall_config["rotation"]["roster"]
    ]

    tz = ZoneInfo(oncall_config["timezone"])
    now_local = now.astimezone(tz)

    if not roster:
        msg = "On-call roster is empty"
        raise ValueError(msg)

    # Check overrides first
    for override in oncall_config.get("overrides", []):
        override_start = datetime.fromisoformat(override["start"])
        override_end = datetime.fromisoformat(override["end"])
        # Coerce naive datetimes to the schedule's configured timezone
        if override_start.tzinfo is None:
            override_start = override_start.replace(tzinfo=tz)
        if override_end.tzinfo is None:
            override_end = override_end.replace(tzinfo=tz)
        if override_start <= now < override_end:
            user_name = override["user"]
            override_user = next(
                (m for m in roster if m.name == user_name), None
            )
            if override_user is None:
                msg = f"Override references unknown roster member: {user_name}"
                raise ValueError(msg)
            idx = roster.index(override_user)
            secondary = roster[(idx + 1) % len(roster)]
            logger.debug(
                "resolve_oncall",
                primary=override_user.name,
                source="override",
                reason=override.get("reason"),
            )
            return OnCallResult(
                primary=override_user,
                secondary=secondary,
                rotation_handoff=override_end,
                source="override",
            )

    # Compute rotation position using a stable epoch
    rotation_type = oncall_config["rotation"]["type"]
    handoff_str = oncall_config["rotation"]["handoff"]

    period = _rotation_period(rotation_type)
    epoch = _compute_epoch(handoff_str, tz)
    most_recent_handoff = _find_last_handoff(handoff_str, tz, now_local)

    elapsed = now_local - epoch
    if elapsed.total_seconds() < 0:
        position = 0
        next_handoff = epoch
    else:
        rotations_elapsed = int(elapsed.total_seconds() // period.total_seconds())
        position = rotations_elapsed % len(roster)
        next_handoff = most_recent_handoff + period

    primary = roster[position]
    secondary = roster[(position + 1) % len(roster)]

    logger.debug(
        "resolve_oncall",
        primary=primary.name,
        secondary=secondary.name,
        source="rotation",
        handoff=next_handoff.isoformat(),
    )
    return OnCallResult(
        primary=primary,
        secondary=secondary,
        rotation_handoff=next_handoff.astimezone(tz),
        source="rotation",
    )


def _rotation_period(rotation_type: str) -> timedelta:
    """Return the duration of one rotation cycle."""
    periods = {
        "weekly": timedelta(weeks=1),
        "daily": timedelta(days=1),
    }
    if rotation_type not in periods:
        msg = f"Unknown rotation type: {rotation_type}"
        raise ValueError(msg)
    return periods[rotation_type]


def _parse_handoff(handoff_str: str) -> tuple[int | None, int, int]:
    """Parse a handoff string into (target_weekday, hour, minute).

    Accepts "monday 09:00" (weekly) or "09:00" (daily).
    Returns ``None`` for target_weekday when daily.
    """
    parts = handoff_str.lower().split()
    if len(parts) == 2:
        day_name, time_str = parts
        if day_name not in DAY_MAP:
            msg = f"Invalid day name in handoff: {day_name!r} (expected one of {', '.join(DAY_MAP)})"
            raise ValueError(msg)
        target_weekday = DAY_MAP[day_name]
    elif len(parts) == 1:
        time_str = parts[0]
        target_weekday = None
    else:
        msg = f"Invalid handoff format: {handoff_str!r} (expected 'monday 09:00' or '09:00')"
        raise ValueError(msg)

    time_parts = time_str.split(":")
    if len(time_parts) != 2:
        msg = f"Invalid time format in handoff: {time_str!r} (expected HH:MM)"
        raise ValueError(msg)

    try:
        hour, minute = int(time_parts[0]), int(time_parts[1])
    except ValueError:
        msg = f"Invalid time in handoff: {time_str!r} (expected numeric HH:MM)"
        raise ValueError(msg) from None

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        msg = f"Time out of range in handoff: {hour:02d}:{minute:02d}"
        raise ValueError(msg)

    return target_weekday, hour, minute


def _compute_epoch(handoff_str: str, tz: ZoneInfo) -> datetime:
    """Compute a stable epoch for rotation counting.

    Uses a fixed reference date (2000-01-03, a Monday) so that
    rotation position is deterministic regardless of when ``now`` is.
    """
    target_weekday, hour, minute = _parse_handoff(handoff_str)

    # 2000-01-03 is a Monday (weekday 0)
    base = datetime(2000, 1, 3, hour, minute, 0, tzinfo=tz)

    if target_weekday is not None:
        # Advance to the target weekday within the first week
        base += timedelta(days=target_weekday)

    return base


def _find_last_handoff(handoff_str: str, tz: ZoneInfo, reference: datetime) -> datetime:
    """Find the most recent handoff time at or before ``reference``.

    For "monday 09:00" with weekly rotation: the most recent Monday 09:00.
    For "09:00" with daily rotation: the most recent 09:00.
    """
    target_weekday, hour, minute = _parse_handoff(handoff_str)

    # Start with today at the handoff time
    candidate = reference.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if target_weekday is not None:
        # Walk back to the target weekday
        days_back = (candidate.weekday() - target_weekday) % 7
        candidate -= timedelta(days=days_back)

    # If candidate is in the future relative to reference, go back one period
    if candidate > reference:
        if target_weekday is not None:
            candidate -= timedelta(weeks=1)
        else:
            candidate -= timedelta(days=1)

    return candidate
