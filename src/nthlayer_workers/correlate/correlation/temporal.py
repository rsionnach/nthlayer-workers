"""Temporal grouping — windowed aggregation by service. Deterministic transport."""
from __future__ import annotations

from datetime import UTC, datetime

from nthlayer_workers.correlate.types import SitRepEvent, TemporalGroup


def _parse_ts(ts: str) -> datetime:
    """Parse ISO 8601 timestamp. Always returns timezone-aware (UTC default)."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def group_temporal(
    events: list[SitRepEvent], window_minutes: int = 5
) -> list[TemporalGroup]:
    """Group events by service within time windows.

    Events on the same service within window_minutes of the earliest
    event in the group are grouped together.
    """
    if not events:
        return []

    # Sort by timestamp
    sorted_events = sorted(events, key=lambda e: e.timestamp)

    # Group by service
    by_service: dict[str, list[SitRepEvent]] = {}
    for event in sorted_events:
        by_service.setdefault(event.service, []).append(event)

    groups: list[TemporalGroup] = []
    window_seconds = window_minutes * 60

    for service, svc_events in by_service.items():
        # Split into windows
        current_window: list[SitRepEvent] = [svc_events[0]]
        window_start = _parse_ts(svc_events[0].timestamp)

        for event in svc_events[1:]:
            event_ts = _parse_ts(event.timestamp)
            if (event_ts - window_start).total_seconds() <= window_seconds:
                current_window.append(event)
            else:
                # Emit current window, start new one
                groups.append(_make_group(service, current_window))
                current_window = [event]
                window_start = event_ts

        # Emit last window
        if current_window:
            groups.append(_make_group(service, current_window))

    return groups


def _make_group(service: str, events: list[SitRepEvent]) -> TemporalGroup:
    """Create a TemporalGroup from a list of events."""
    first_ts = events[0].timestamp
    last_ts = events[-1].timestamp
    try:
        start = _parse_ts(first_ts)
        end = _parse_ts(last_ts)
        duration = (end - start).total_seconds()
    except (ValueError, TypeError):
        duration = 0.0

    return TemporalGroup(
        service=service,
        time_window=(first_ts, last_ts),
        events=events,
        count=len(events),
        peak_severity=max(e.severity for e in events),
        duration_seconds=duration,
    )
