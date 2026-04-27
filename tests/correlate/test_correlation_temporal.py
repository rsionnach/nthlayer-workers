"""Tests for temporal grouping."""
from __future__ import annotations

from nthlayer_workers.correlate.correlation.temporal import group_temporal
from nthlayer_workers.correlate.types import EventType, SitRepEvent


def _make_event(service="api", ts="2026-03-01T14:00:00Z", severity=0.5):
    return SitRepEvent(
        id=f"evt-{ts}-{service}",
        timestamp=ts,
        source="prometheus",
        type=EventType.ALERT,
        service=service,
        environment="production",
        severity=severity,
        payload={"alert_name": "test"},
    )


class TestGroupTemporal:
    def test_same_service_within_window(self):
        events = [
            _make_event(ts="2026-03-01T14:00:00Z"),
            _make_event(ts="2026-03-01T14:02:00Z"),
            _make_event(ts="2026-03-01T14:04:00Z"),
        ]
        groups = group_temporal(events, window_minutes=5)
        assert len(groups) == 1
        assert groups[0].count == 3

    def test_different_services_separate_groups(self):
        events = [
            _make_event(service="api-a", ts="2026-03-01T14:00:00Z"),
            _make_event(service="api-b", ts="2026-03-01T14:01:00Z"),
        ]
        groups = group_temporal(events, window_minutes=5)
        assert len(groups) == 2

    def test_peak_severity(self):
        events = [
            _make_event(severity=0.3, ts="2026-03-01T14:00:00Z"),
            _make_event(severity=0.9, ts="2026-03-01T14:01:00Z"),
            _make_event(severity=0.5, ts="2026-03-01T14:02:00Z"),
        ]
        groups = group_temporal(events, window_minutes=5)
        assert groups[0].peak_severity == 0.9

    def test_duration_calculation(self):
        events = [
            _make_event(ts="2026-03-01T14:00:00Z"),
            _make_event(ts="2026-03-01T14:05:00Z"),
        ]
        groups = group_temporal(events, window_minutes=10)
        assert groups[0].duration_seconds == 300.0

    def test_events_outside_window_separate_groups(self):
        events = [
            _make_event(ts="2026-03-01T14:00:00Z"),
            _make_event(ts="2026-03-01T14:10:00Z"),  # 10 min later
        ]
        groups = group_temporal(events, window_minutes=5)
        assert len(groups) == 2

    def test_empty_events(self):
        assert group_temporal([]) == []
