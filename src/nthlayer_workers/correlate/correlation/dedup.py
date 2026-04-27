"""Signal deduplication. Deterministic transport."""
from __future__ import annotations

from dataclasses import replace

from nthlayer_workers.correlate.types import SitRepEvent


def _dedup_key(event: SitRepEvent) -> str:
    """Build dedup key from event fields."""
    parts = [event.source, event.service, event.type.value, event.environment]
    # Add alert_name or metric from payload if present
    alert_name = event.payload.get("alert_name") or event.payload.get("metric")
    if alert_name:
        parts.append(str(alert_name))
    return "|".join(parts)


def deduplicate(events: list[SitRepEvent]) -> list[SitRepEvent]:
    """Collapse events with the same dedup key.

    Returns deduplicated list. The first occurrence of each key is kept,
    with _dedup_count and _dedup_duration_seconds added to its payload.
    """
    seen: dict[str, SitRepEvent] = {}
    counts: dict[str, int] = {}
    first_ts: dict[str, str] = {}
    last_ts: dict[str, str] = {}

    for event in events:
        key = _dedup_key(event)
        if key not in seen:
            seen[key] = event
            counts[key] = 1
            first_ts[key] = event.timestamp
            last_ts[key] = event.timestamp
        else:
            counts[key] += 1
            if event.timestamp < first_ts[key]:
                first_ts[key] = event.timestamp
            if event.timestamp > last_ts[key]:
                last_ts[key] = event.timestamp

    result = []
    for key, event in seen.items():
        if counts[key] > 1:
            # Add dedup metadata to payload (don't mutate original)
            from datetime import datetime
            try:
                start = datetime.fromisoformat(first_ts[key].replace("Z", "+00:00"))
                end = datetime.fromisoformat(last_ts[key].replace("Z", "+00:00"))
                duration = (end - start).total_seconds()
            except (ValueError, TypeError):
                duration = 0.0

            new_payload = dict(event.payload)
            new_payload["_dedup_count"] = counts[key]
            new_payload["_dedup_duration_seconds"] = duration
            event = replace(event, payload=new_payload)
        result.append(event)

    return result
