"""Tests for session window logic — CorrelationDomain, SessionWindow, SessionWindowManager."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nthlayer_workers.correlate.session import (
    CorrelationDomain,
    SessionWindow,
    SessionWindowManager,
)
from nthlayer_workers.correlate.types import EventType, SitRepEvent


def _event(
    service: str = "fraud-detect",
    environment: str = "production",
    timestamp: str = "2026-04-24T12:00:00+00:00",
    source: str = "assessment:slo_status",
    event_type: EventType = EventType.ALERT,
    severity: float = 0.5,
) -> SitRepEvent:
    return SitRepEvent(
        id=f"evt-{timestamp}",
        timestamp=timestamp,
        source=source,
        type=event_type,
        service=service,
        environment=environment,
        severity=severity,
        payload={},
    )


class TestCorrelationDomain:
    def test_from_event(self):
        e = _event(service="svc-a", environment="staging")
        domain = CorrelationDomain.from_event(e)
        assert domain.service == "svc-a"
        assert domain.environment == "staging"

    def test_equality(self):
        d1 = CorrelationDomain("svc", "prod")
        d2 = CorrelationDomain("svc", "prod")
        assert d1 == d2

    def test_different_environments_not_equal(self):
        d1 = CorrelationDomain("svc", "production")
        d2 = CorrelationDomain("svc", "staging")
        assert d1 != d2

    def test_hashable(self):
        d = CorrelationDomain("svc", "prod")
        s = {d}
        assert d in s

    def test_str(self):
        assert str(CorrelationDomain("svc", "prod")) == "svc:prod"


class TestSessionWindow:
    def test_closes_on_gap(self):
        now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=timezone.utc)
        window = SessionWindow(
            domain=CorrelationDomain("svc", "prod"),
            opened_at=now - timedelta(seconds=30),
            last_event_at=now - timedelta(seconds=61),
        )
        assert window.should_close(now, gap_seconds=60.0) is True
        assert window.close_reason(now, gap_seconds=60.0) == "gap"

    def test_does_not_close_before_gap(self):
        now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=timezone.utc)
        window = SessionWindow(
            domain=CorrelationDomain("svc", "prod"),
            opened_at=now - timedelta(seconds=30),
            last_event_at=now - timedelta(seconds=50),
        )
        assert window.should_close(now, gap_seconds=60.0) is False

    def test_closes_on_max_duration(self):
        now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=timezone.utc)
        window = SessionWindow(
            domain=CorrelationDomain("svc", "prod"),
            opened_at=now - timedelta(minutes=16),
            last_event_at=now - timedelta(seconds=5),
        )
        assert window.should_close(now, max_duration_seconds=900.0) is True
        assert window.close_reason(now, max_duration_seconds=900.0) == "max_duration"

    def test_closes_on_trigger(self):
        now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=timezone.utc)
        window = SessionWindow(
            domain=CorrelationDomain("svc", "prod"),
            opened_at=now - timedelta(seconds=10),
            last_event_at=now - timedelta(seconds=5),
            has_trigger=True,
        )
        assert window.should_close(now) is True
        assert window.close_reason(now) == "trigger"

    def test_add_event_updates_last_event_at(self):
        now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=timezone.utc)
        window = SessionWindow(
            domain=CorrelationDomain("svc", "prod"),
            opened_at=now,
            last_event_at=now,
        )
        later = now + timedelta(seconds=30)
        window.add_event(_event(timestamp=later.isoformat()))
        assert window.last_event_at == later
        assert len(window.events) == 1


class TestSessionWindowManager:
    def test_ingest_opens_window(self):
        mgr = SessionWindowManager()
        mgr.ingest(_event())
        assert len(mgr.open_windows) == 1

    def test_ingest_same_domain_reuses_window(self):
        mgr = SessionWindowManager()
        mgr.ingest(_event(timestamp="2026-04-24T12:00:00+00:00"))
        mgr.ingest(_event(timestamp="2026-04-24T12:00:10+00:00"))
        assert len(mgr.open_windows) == 1
        domain = CorrelationDomain("fraud-detect", "production")
        assert len(mgr.open_windows[domain].events) == 2

    def test_different_domains_separate_windows(self):
        mgr = SessionWindowManager()
        mgr.ingest(_event(service="svc-a"))
        mgr.ingest(_event(service="svc-b"))
        assert len(mgr.open_windows) == 2

    def test_different_environments_separate_windows(self):
        mgr = SessionWindowManager()
        mgr.ingest(_event(environment="production"))
        mgr.ingest(_event(environment="staging"))
        assert len(mgr.open_windows) == 2

    def test_close_ready_returns_closed_windows(self):
        mgr = SessionWindowManager(gap_seconds=60.0)
        now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=timezone.utc)
        mgr.ingest(_event(timestamp=(now - timedelta(seconds=120)).isoformat()))

        closed = mgr.close_ready(now)
        assert len(closed) == 1
        assert len(mgr.open_windows) == 0

    def test_close_ready_leaves_active_windows(self):
        mgr = SessionWindowManager(gap_seconds=60.0)
        now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=timezone.utc)
        mgr.ingest(_event(timestamp=(now - timedelta(seconds=10)).isoformat()))

        closed = mgr.close_ready(now)
        assert len(closed) == 0
        assert len(mgr.open_windows) == 1

    def test_trigger_verdict_marks_window(self):
        mgr = SessionWindowManager()
        mgr.ingest(_event(source="verdict:quality_breach"))
        domain = CorrelationDomain("fraud-detect", "production")
        assert mgr.open_windows[domain].has_trigger is True

    def test_to_state_and_restore(self):
        mgr = SessionWindowManager()
        now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=timezone.utc)
        mgr.ingest(_event(timestamp=now.isoformat()))

        state = mgr.to_state()
        assert "fraud-detect:production" in state

        mgr2 = SessionWindowManager()
        for key, metadata in state.items():
            parts = key.split(":", 1)
            domain = CorrelationDomain(parts[0], parts[1])
            mgr2.restore_window(domain, metadata)

        assert len(mgr2.open_windows) == 1
        restored = mgr2.open_windows[CorrelationDomain("fraud-detect", "production")]
        assert restored.opened_at == now
        assert restored.last_event_at == now
        assert restored.has_trigger is False
        assert restored.events == []  # events not persisted, re-fetched separately
