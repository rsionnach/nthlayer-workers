"""Built-in condition functions for policy evaluation.

Provides time-based, date-based, and service-based conditions.
All functions are pure — no external data access.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any


def get_current_context(
    budget_remaining: float = 100.0,
    budget_consumed: float = 0.0,
    burn_rate: float = 1.0,
    tier: str = "standard",
    environment: str = "prod",
    downstream_count: int = 0,
    high_criticality_downstream: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build context dictionary for condition evaluation."""
    if now is None:
        now = datetime.now()

    return {
        "hour": now.hour,
        "minute": now.minute,
        "weekday": now.weekday() < 5,
        "day_of_week": now.weekday(),
        "date": now.date().isoformat(),
        "month": now.month,
        "day": now.day,
        "year": now.year,
        "budget_remaining": budget_remaining,
        "budget_consumed": budget_consumed,
        "burn_rate": burn_rate,
        "tier": tier,
        "environment": environment,
        "env": environment,
        "downstream_count": downstream_count,
        "high_criticality_downstream": high_criticality_downstream,
    }


def is_business_hours(
    now: datetime | None = None,
    start_hour: int = 9,
    end_hour: int = 17,
) -> bool:
    """Check if current time is within business hours (Mon-Fri, 9-17)."""
    if now is None:
        now = datetime.now()
    if now.weekday() >= 5:
        return False
    return start_hour <= now.hour < end_hour


def is_weekday(now: datetime | None = None) -> bool:
    """Check if current day is a weekday (Mon-Fri)."""
    if now is None:
        now = datetime.now()
    return now.weekday() < 5


def is_freeze_period(
    start_date: str,
    end_date: str,
    now: datetime | None = None,
) -> bool:
    """Check if current date is within a freeze period."""
    if now is None:
        now = datetime.now()
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        return start <= now.date() <= end
    except ValueError:
        return False


def is_peak_traffic(
    now: datetime | None = None,
    peak_hours: list[tuple[int, int]] | None = None,
) -> bool:
    """Check if current time is during peak traffic hours."""
    if now is None:
        now = datetime.now()
    if peak_hours is None:
        peak_hours = [(10, 12), (14, 16)]
    return any(start <= now.hour < end for start, end in peak_hours)
