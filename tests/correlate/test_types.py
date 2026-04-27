"""Tests for SitRep core data types."""
from __future__ import annotations

from nthlayer_workers.correlate.types import (
    AgentState,
    CorrelationGroup,
    EventType,
    SitRepEvent,
    TemporalGroup,
)


class TestSitRepEvent:
    def test_create_alert_event(self):
        event = SitRepEvent(
            id="evt-001",
            timestamp="2026-03-01T14:00:00Z",
            source="prometheus",
            type=EventType.ALERT,
            service="payment-api",
            environment="production",
            severity=0.8,
            payload={"alert_name": "latency_breach"},
        )
        assert event.service == "payment-api"
        assert event.type == EventType.ALERT
        assert event.severity == 0.8

    def test_default_fields(self):
        event = SitRepEvent(
            id="evt-002",
            timestamp="2026-03-01T14:00:00Z",
            source="github",
            type=EventType.CHANGE,
            service="api",
            environment="production",
            severity=0.5,
            payload={},
        )
        assert event.dependencies == []
        assert event.dependents == []
        assert event.ttl == 86400


class TestEventType:
    def test_all_types_exist(self):
        assert EventType.ALERT == "alert"
        assert EventType.METRIC_BREACH == "metric_breach"
        assert EventType.CHANGE == "change"
        assert EventType.QUALITY_SCORE == "quality_score"
        assert EventType.VERDICT == "verdict"
        assert EventType.CUSTOM == "custom"


class TestAgentState:
    def test_all_states(self):
        assert AgentState.WATCHING == "watching"
        assert AgentState.ALERT == "alert"
        assert AgentState.INCIDENT == "incident"
        assert AgentState.DEGRADED == "degraded"


class TestTemporalGroup:
    def test_create(self):
        group = TemporalGroup(
            service="payment-api",
            time_window=("2026-03-01T14:00:00Z", "2026-03-01T14:05:00Z"),
            events=[],
            count=3,
            peak_severity=0.9,
            duration_seconds=300.0,
        )
        assert group.count == 3
        assert group.peak_severity == 0.9


class TestCorrelationGroup:
    def test_create(self):
        group = CorrelationGroup(
            id="cg-001",
            priority=0,
            summary="3 alerts on payment-api",
            services=["payment-api"],
            signals=[],
            topology=None,
            change_candidates=[],
            first_seen="2026-03-01T14:00:00Z",
            last_updated="2026-03-01T14:05:00Z",
            event_count=3,
        )
        assert group.priority == 0
        assert group.topology is None
