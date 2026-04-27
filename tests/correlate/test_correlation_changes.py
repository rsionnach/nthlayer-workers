"""Tests for change candidate indexing."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from nthlayer_workers.correlate.correlation.changes import find_change_candidates
from nthlayer_workers.correlate.types import EventType, SitRepEvent, TemporalGroup


def _make_event(
    id,
    service="payment-api",
    ts="2026-03-01T14:12:00Z",
    severity=0.8,
    etype=EventType.ALERT,
    payload=None,
):
    return SitRepEvent(
        id=id,
        timestamp=ts,
        source="prometheus",
        type=etype,
        service=service,
        environment="production",
        severity=severity,
        payload=payload or {"alert_name": "latency_p99_breach"},
    )


def _make_temporal_group(service, events, peak_severity=0.8):
    first_ts = events[0].timestamp
    last_ts = events[-1].timestamp
    return TemporalGroup(
        service=service,
        time_window=(first_ts, last_ts),
        events=events,
        count=len(events),
        peak_severity=peak_severity,
        duration_seconds=0.0,
    )


class TestFindChangeCandidates:
    def test_same_service_change_found(self, sqlite_store):
        """Alert on service X, change on same service 8 min ago -> ChangeCandidate."""
        # Use timestamps relative to "now" so SQLite datetime('now') works
        now = datetime.now(timezone.utc)
        change_ts = (now - timedelta(minutes=8)).isoformat()
        alert_ts = now.isoformat()

        change_event = _make_event(
            id="evt-change-001",
            service="payment-api",
            ts=change_ts,
            severity=0.3,
            etype=EventType.CHANGE,
            payload={"change_type": "deploy", "to_version": "2.3.1"},
        )
        alert_event = _make_event(
            id="evt-alert-001",
            service="payment-api",
            ts=alert_ts,
        )

        sqlite_store.insert(change_event)
        sqlite_store.insert(alert_event)

        group = _make_temporal_group("payment-api", [alert_event])

        result = find_change_candidates(
            sqlite_store, [group], window_minutes=30
        )
        assert "payment-api" in result
        candidates = result["payment-api"]
        assert len(candidates) >= 1
        assert candidates[0].same_service is True
        # 8 minutes = ~480 seconds
        assert 400 < candidates[0].temporal_proximity_seconds < 560

    def test_dependency_change_found(self, sqlite_store):
        """Alert on service X, change on dependency Y -> dependency_related=True."""
        now = datetime.now(timezone.utc)
        change_ts = (now - timedelta(minutes=5)).isoformat()
        alert_ts = now.isoformat()

        change_event = _make_event(
            id="evt-change-dep",
            service="database-primary",
            ts=change_ts,
            severity=0.3,
            etype=EventType.CHANGE,
            payload={"change_type": "config", "detail": "connection pool resize"},
        )
        alert_event = _make_event(
            id="evt-alert-dep",
            service="payment-api",
            ts=alert_ts,
        )

        sqlite_store.insert(change_event)
        sqlite_store.insert(alert_event)

        topology = {
            "payment-api": {
                "tier": "critical",
                "dependencies": ["database-primary"],
                "dependents": [],
            },
            "database-primary": {
                "tier": "critical",
                "dependencies": [],
                "dependents": ["payment-api"],
            },
        }

        group = _make_temporal_group("payment-api", [alert_event])

        result = find_change_candidates(
            sqlite_store, [group], topology=topology, window_minutes=30
        )
        assert "payment-api" in result
        dep_candidates = [c for c in result["payment-api"] if c.dependency_related]
        assert len(dep_candidates) >= 1

    def test_no_recent_changes_empty(self, sqlite_store):
        """No recent changes -> empty list."""
        now = datetime.now(timezone.utc)
        alert_ts = now.isoformat()

        alert_event = _make_event(
            id="evt-alert-lonely",
            service="payment-api",
            ts=alert_ts,
        )
        sqlite_store.insert(alert_event)

        group = _make_temporal_group("payment-api", [alert_event])

        result = find_change_candidates(
            sqlite_store, [group], window_minutes=30
        )
        # No changes exist, so either key absent or empty list
        candidates = result.get("payment-api", [])
        assert len(candidates) == 0

    def test_changes_sorted_by_proximity(self, sqlite_store):
        """Changes sorted by proximity (closest first)."""
        now = datetime.now(timezone.utc)
        change_ts_far = (now - timedelta(minutes=20)).isoformat()
        change_ts_close = (now - timedelta(minutes=3)).isoformat()
        alert_ts = now.isoformat()

        change_far = _make_event(
            id="evt-change-far",
            service="payment-api",
            ts=change_ts_far,
            severity=0.3,
            etype=EventType.CHANGE,
            payload={"change_type": "deploy", "to_version": "2.3.0"},
        )
        change_close = _make_event(
            id="evt-change-close",
            service="payment-api",
            ts=change_ts_close,
            severity=0.3,
            etype=EventType.CHANGE,
            payload={"change_type": "deploy", "to_version": "2.3.1"},
        )
        alert_event = _make_event(
            id="evt-alert-sort",
            service="payment-api",
            ts=alert_ts,
        )

        sqlite_store.insert(change_far)
        sqlite_store.insert(change_close)
        sqlite_store.insert(alert_event)

        group = _make_temporal_group("payment-api", [alert_event])

        result = find_change_candidates(
            sqlite_store, [group], window_minutes=30
        )
        candidates = result["payment-api"]
        assert len(candidates) == 2
        # Closest first
        assert candidates[0].temporal_proximity_seconds < candidates[1].temporal_proximity_seconds
