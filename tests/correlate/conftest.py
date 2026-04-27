"""Shared test fixtures for SitRep."""
from __future__ import annotations

import pytest
from nthlayer_workers.correlate.store.sqlite import SQLiteEventStore
from nthlayer_workers.correlate.types import EventType, SitRepEvent


@pytest.fixture
def sqlite_store(tmp_path):
    store = SQLiteEventStore(str(tmp_path / "test-events.db"))
    yield store
    store.close()


@pytest.fixture
def sample_alert_event():
    return SitRepEvent(
        id="evt-test-001",
        timestamp="2026-03-01T14:12:00Z",
        source="prometheus",
        type=EventType.ALERT,
        service="payment-api",
        environment="production",
        severity=0.8,
        payload={"alert_name": "latency_p99_breach", "value": 0.52, "threshold": 0.20},
    )


@pytest.fixture
def sample_change_event():
    return SitRepEvent(
        id="evt-test-002",
        timestamp="2026-03-01T14:00:00Z",
        source="github",
        type=EventType.CHANGE,
        service="payment-api",
        environment="production",
        severity=0.3,
        payload={"change_type": "deploy", "to_version": "2.3.1"},
        ttl=604800,  # 7 days for changes
    )
