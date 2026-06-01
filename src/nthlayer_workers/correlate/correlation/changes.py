"""Change candidate indexing. Deterministic transport."""
from __future__ import annotations

from datetime import UTC, datetime

from nthlayer_workers.correlate.store.protocol import EventStore
from nthlayer_workers.correlate.types import ChangeCandidate, TemporalGroup


def _parse_ts(ts: str) -> datetime:
    """Parse ISO 8601 timestamp. Always returns timezone-aware (UTC default)."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def find_change_candidates(
    store: EventStore,
    temporal_groups: list[TemporalGroup],
    topology: dict | None = None,
    window_minutes: int = 30,
) -> dict[str, list[ChangeCandidate]]:
    """For each temporal group, find recent changes on the same service or dependencies.

    Returns mapping from service name to list of change candidates,
    sorted by temporal proximity (closest first).
    """
    result: dict[str, list[ChangeCandidate]] = {}

    for group in temporal_groups:
        service = group.service
        candidates: list[ChangeCandidate] = []

        # Parse group's first event timestamp for proximity calculation
        group_first_ts = _parse_ts(group.time_window[0])

        # Use the group's first event as reference time for "recent" lookback.
        # This is essential for replay with historical timestamps — without it,
        # get_recent_changes uses datetime('now') and finds nothing.
        reference_time = group.time_window[0]

        # Check for changes on the same service
        same_service_changes = store.get_recent_changes(
            service, window_minutes, reference_time=reference_time
        )
        for change in same_service_changes:
            change_ts = _parse_ts(change.timestamp)
            proximity = abs((group_first_ts - change_ts).total_seconds())
            candidates.append(
                ChangeCandidate(
                    change=change,
                    affected_service=service,
                    temporal_proximity_seconds=proximity,
                    same_service=True,
                    dependency_related=False,
                )
            )

        # Check for changes on dependency services
        if topology is not None:
            svc_info = topology.get(service, {})
            dependencies = svc_info.get("dependencies", [])
            for dep_service in dependencies:
                dep_changes = store.get_recent_changes(
                    dep_service, window_minutes, reference_time=reference_time
                )
                for change in dep_changes:
                    change_ts = _parse_ts(change.timestamp)
                    proximity = abs((group_first_ts - change_ts).total_seconds())
                    candidates.append(
                        ChangeCandidate(
                            change=change,
                            affected_service=service,
                            temporal_proximity_seconds=proximity,
                            same_service=False,
                            dependency_related=True,
                        )
                    )

        # Sort by temporal proximity (closest first)
        candidates.sort(key=lambda c: c.temporal_proximity_seconds)

        if candidates:
            result[service] = candidates

    return result
