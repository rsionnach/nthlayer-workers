"""Tests for signal deduplication."""
from __future__ import annotations

from nthlayer_workers.correlate.correlation.dedup import deduplicate
from nthlayer_workers.correlate.types import EventType, SitRepEvent


def _make_alert(service="api", alert_name="latency", ts="2026-03-01T14:00:00Z", env="prod"):
    return SitRepEvent(
        id=f"evt-{ts}-{service}-{alert_name}",
        timestamp=ts,
        source="prometheus",
        type=EventType.ALERT,
        service=service,
        environment=env,
        severity=0.8,
        payload={"alert_name": alert_name},
    )


class TestDeduplicate:
    def test_same_key_collapsed(self):
        events = [
            _make_alert(ts="2026-03-01T14:00:00Z"),
            _make_alert(ts="2026-03-01T14:01:00Z"),
            _make_alert(ts="2026-03-01T14:02:00Z"),
        ]
        result = deduplicate(events)
        assert len(result) == 1
        assert result[0].payload["_dedup_count"] == 3

    def test_different_services_not_collapsed(self):
        events = [
            _make_alert(service="api-a"),
            _make_alert(service="api-b"),
        ]
        result = deduplicate(events)
        assert len(result) == 2

    def test_different_alerts_not_collapsed(self):
        events = [
            _make_alert(alert_name="latency"),
            _make_alert(alert_name="error_rate"),
        ]
        result = deduplicate(events)
        assert len(result) == 2

    def test_single_event_no_metadata(self):
        events = [_make_alert()]
        result = deduplicate(events)
        assert len(result) == 1
        assert "_dedup_count" not in result[0].payload

    def test_empty_list(self):
        assert deduplicate([]) == []

    def test_dedup_duration_calculated(self):
        events = [
            _make_alert(ts="2026-03-01T14:00:00Z"),
            _make_alert(ts="2026-03-01T14:05:00Z"),
        ]
        result = deduplicate(events)
        assert result[0].payload["_dedup_duration_seconds"] == 300.0
