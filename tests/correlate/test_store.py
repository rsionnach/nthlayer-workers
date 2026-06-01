"""Tests for EventStore protocol and SQLite FTS5 implementation."""
from __future__ import annotations

from datetime import UTC, datetime

from nthlayer_workers.correlate.store.sqlite import SQLiteEventStore
from nthlayer_workers.correlate.types import EventType, SitRepEvent


class TestInsertAndQuery:
    def test_insert_and_get_by_time_window(self, sqlite_store, sample_alert_event):
        sqlite_store.insert(sample_alert_event)
        results = sqlite_store.get_by_time_window(
            "2026-03-01T14:00:00Z", "2026-03-01T15:00:00Z"
        )
        assert len(results) == 1
        assert results[0].id == "evt-test-001"
        assert results[0].type == EventType.ALERT

    def test_query_with_service_filter(self, sqlite_store, sample_alert_event):
        sqlite_store.insert(sample_alert_event)
        results = sqlite_store.get_by_time_window(
            "2026-03-01T14:00:00Z",
            "2026-03-01T15:00:00Z",
            service="payment-api",
        )
        assert len(results) == 1
        results = sqlite_store.get_by_time_window(
            "2026-03-01T14:00:00Z",
            "2026-03-01T15:00:00Z",
            service="other-service",
        )
        assert len(results) == 0

    def test_query_with_type_filter(
        self, sqlite_store, sample_alert_event, sample_change_event
    ):
        sqlite_store.insert(sample_alert_event)
        sqlite_store.insert(sample_change_event)
        results = sqlite_store.get_by_time_window(
            "2026-03-01T13:00:00Z",
            "2026-03-01T15:00:00Z",
            event_type=EventType.ALERT,
        )
        assert len(results) == 1
        assert results[0].type == EventType.ALERT

    def test_query_with_min_severity(
        self, sqlite_store, sample_alert_event, sample_change_event
    ):
        sqlite_store.insert(sample_alert_event)  # severity 0.8
        sqlite_store.insert(sample_change_event)  # severity 0.3
        results = sqlite_store.get_by_time_window(
            "2026-03-01T13:00:00Z",
            "2026-03-01T15:00:00Z",
            min_severity=0.5,
        )
        assert len(results) == 1
        assert results[0].severity == 0.8

    def test_roundtrip_preserves_all_fields(self, sqlite_store):
        event = SitRepEvent(
            id="evt-roundtrip",
            timestamp="2026-03-01T14:00:00Z",
            source="github",
            type=EventType.CHANGE,
            service="payment-api",
            environment="staging",
            severity=0.6,
            payload={"change_type": "deploy", "version": "1.2.3"},
            dependencies=["db-service", "cache-service"],
            dependents=["frontend"],
            ttl=604800,
        )
        sqlite_store.insert(event)
        results = sqlite_store.get_by_time_window(
            "2026-03-01T13:00:00Z", "2026-03-01T15:00:00Z"
        )
        assert len(results) == 1
        r = results[0]
        assert r.id == "evt-roundtrip"
        assert r.source == "github"
        assert r.type == EventType.CHANGE
        assert r.service == "payment-api"
        assert r.environment == "staging"
        assert r.severity == 0.6
        assert r.payload == {"change_type": "deploy", "version": "1.2.3"}
        assert r.dependencies == ["db-service", "cache-service"]
        assert r.dependents == ["frontend"]
        assert r.ttl == 604800


class TestSearch:
    def test_fts_search(self, sqlite_store, sample_alert_event):
        sqlite_store.insert(sample_alert_event)
        results = sqlite_store.search("latency_p99_breach")
        assert len(results) >= 1

    def test_fts_search_with_service(self, sqlite_store, sample_alert_event):
        sqlite_store.insert(sample_alert_event)
        results = sqlite_store.search("latency", service="payment-api")
        assert len(results) >= 1
        results = sqlite_store.search("latency", service="other")
        assert len(results) == 0

    def test_fts_search_with_time_window(self, sqlite_store, sample_alert_event):
        sqlite_store.insert(sample_alert_event)
        results = sqlite_store.search(
            "latency",
            time_window=("2026-03-01T14:00:00Z", "2026-03-01T15:00:00Z"),
        )
        assert len(results) >= 1
        results = sqlite_store.search(
            "latency",
            time_window=("2025-01-01T00:00:00Z", "2025-01-01T01:00:00Z"),
        )
        assert len(results) == 0

    def test_fts_search_respects_limit(self, sqlite_store):
        for i in range(10):
            event = SitRepEvent(
                id=f"evt-search-{i}",
                timestamp="2026-03-01T14:00:00Z",
                source="test",
                type=EventType.ALERT,
                service="svc",
                environment="prod",
                severity=0.5,
                payload={"alert_name": "common_alert"},
            )
            sqlite_store.insert(event)
        results = sqlite_store.search("common_alert", limit=3)
        assert len(results) == 3


class TestTopology:
    def test_get_by_topology_direct(self, sqlite_store):
        event = SitRepEvent(
            id="evt-topo-1",
            timestamp="2026-03-01T14:00:00Z",
            source="test",
            type=EventType.ALERT,
            service="payment-api",
            environment="prod",
            severity=0.5,
            payload={},
            dependencies=["db-service"],
            dependents=["frontend"],
        )
        sqlite_store.insert(event)
        results = sqlite_store.get_by_topology("payment-api", hops=1)
        assert len(results) >= 1

    def test_get_by_topology_via_dependency(self, sqlite_store):
        event = SitRepEvent(
            id="evt-topo-2",
            timestamp="2026-03-01T14:00:00Z",
            source="test",
            type=EventType.ALERT,
            service="db-service",
            environment="prod",
            severity=0.7,
            payload={},
        )
        dep_event = SitRepEvent(
            id="evt-topo-3",
            timestamp="2026-03-01T14:01:00Z",
            source="test",
            type=EventType.ALERT,
            service="payment-api",
            environment="prod",
            severity=0.5,
            payload={},
            dependencies=["db-service"],
        )
        sqlite_store.insert(event)
        sqlite_store.insert(dep_event)
        # Querying topology for db-service should find events where db-service
        # is the service OR is in dependencies/dependents
        results = sqlite_store.get_by_topology("db-service", hops=1)
        assert len(results) >= 1


class TestRecentChanges:
    def test_get_recent_changes(self, sqlite_store):
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        change = SitRepEvent(
            id="evt-recent-change",
            timestamp=now,
            source="github",
            type=EventType.CHANGE,
            service="payment-api",
            environment="production",
            severity=0.3,
            payload={"change_type": "deploy"},
        )
        alert = SitRepEvent(
            id="evt-recent-alert",
            timestamp=now,
            source="prometheus",
            type=EventType.ALERT,
            service="payment-api",
            environment="production",
            severity=0.8,
            payload={"alert_name": "latency"},
        )
        sqlite_store.insert(change)
        sqlite_store.insert(alert)
        changes = sqlite_store.get_recent_changes("payment-api", window_minutes=60)
        assert len(changes) == 1
        assert changes[0].type == EventType.CHANGE

    def test_get_recent_changes_empty(self, sqlite_store):
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        alert = SitRepEvent(
            id="evt-no-change",
            timestamp=now,
            source="prometheus",
            type=EventType.ALERT,
            service="payment-api",
            environment="production",
            severity=0.8,
            payload={"alert_name": "latency"},
        )
        sqlite_store.insert(alert)
        changes = sqlite_store.get_recent_changes("payment-api", window_minutes=60)
        assert len(changes) == 0


class TestExpiry:
    def test_expire_old_events(self, sqlite_store):
        # Insert event with 1-second TTL
        event = SitRepEvent(
            id="evt-expire",
            timestamp="2020-01-01T00:00:00Z",
            source="test",
            type=EventType.ALERT,
            service="svc",
            environment="prod",
            severity=0.5,
            payload={},
            ttl=1,
        )
        sqlite_store.insert(event)
        # Manually backdate created_at so the event is expired
        sqlite_store._conn.execute(
            "UPDATE events SET created_at = datetime('now', '-1 hour') WHERE id = ?",
            ("evt-expire",),
        )
        sqlite_store._conn.commit()
        deleted = sqlite_store.expire_old()
        assert deleted >= 1
        # Verify it's gone
        results = sqlite_store.get_by_time_window(
            "2019-01-01T00:00:00Z", "2021-01-01T00:00:00Z"
        )
        assert len(results) == 0

    def test_expire_keeps_fresh_events(self, sqlite_store, sample_alert_event):
        sqlite_store.insert(sample_alert_event)  # ttl=86400, created just now
        deleted = sqlite_store.expire_old()
        assert deleted == 0
        results = sqlite_store.get_by_time_window(
            "2026-03-01T14:00:00Z", "2026-03-01T15:00:00Z"
        )
        assert len(results) == 1


class TestStateHash:
    def test_same_events_same_hash(self, sqlite_store, sample_alert_event):
        sqlite_store.insert(sample_alert_event)
        h1 = sqlite_store.get_state_hash(
            ("2026-03-01T14:00:00Z", "2026-03-01T15:00:00Z")
        )
        h2 = sqlite_store.get_state_hash(
            ("2026-03-01T14:00:00Z", "2026-03-01T15:00:00Z")
        )
        assert h1 == h2

    def test_different_events_different_hash(
        self, sqlite_store, sample_alert_event, sample_change_event
    ):
        sqlite_store.insert(sample_alert_event)
        h1 = sqlite_store.get_state_hash(
            ("2026-03-01T14:00:00Z", "2026-03-01T15:00:00Z")
        )
        sqlite_store.insert(sample_change_event)
        h2 = sqlite_store.get_state_hash(
            ("2026-03-01T14:00:00Z", "2026-03-01T15:00:00Z")
        )
        assert h1 != h2

    def test_empty_window_hash(self, sqlite_store):
        h = sqlite_store.get_state_hash(
            ("2020-01-01T00:00:00Z", "2020-01-01T01:00:00Z")
        )
        # Should still return a valid hash (of empty string or empty list)
        assert isinstance(h, str)
        assert len(h) == 64  # SHA256 hex digest length


class TestBatchInsert:
    def test_insert_batch(self, sqlite_store):
        events = [
            SitRepEvent(
                id=f"evt-batch-{i}",
                timestamp="2026-03-01T14:00:00Z",
                source="test",
                type=EventType.ALERT,
                service="svc",
                environment="prod",
                severity=0.5,
                payload={},
            )
            for i in range(5)
        ]
        sqlite_store.insert_batch(events)
        results = sqlite_store.get_by_time_window(
            "2026-03-01T13:00:00Z", "2026-03-01T15:00:00Z"
        )
        assert len(results) == 5

    def test_insert_batch_empty(self, sqlite_store):
        sqlite_store.insert_batch([])
        stats = sqlite_store.get_stats()
        assert stats["event_count"] == 0


class TestGetStats:
    def test_stats_empty(self, sqlite_store):
        stats = sqlite_store.get_stats()
        assert stats["event_count"] == 0

    def test_stats_with_events(self, sqlite_store, sample_alert_event):
        sqlite_store.insert(sample_alert_event)
        stats = sqlite_store.get_stats()
        assert stats["event_count"] == 1
        assert "min_timestamp" in stats
        assert "max_timestamp" in stats


class TestContextManager:
    def test_context_manager(self, tmp_path):
        db_path = str(tmp_path / "ctx-test.db")
        with SQLiteEventStore(db_path) as store:
            event = SitRepEvent(
                id="evt-ctx",
                timestamp="2026-03-01T14:00:00Z",
                source="test",
                type=EventType.ALERT,
                service="svc",
                environment="prod",
                severity=0.5,
                payload={},
            )
            store.insert(event)
            results = store.get_by_time_window(
                "2026-03-01T13:00:00Z", "2026-03-01T15:00:00Z"
            )
            assert len(results) == 1
