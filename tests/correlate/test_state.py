"""Tests for agent state machine."""
from __future__ import annotations

from nthlayer_workers.correlate.state import StateMachine
from nthlayer_workers.correlate.types import AgentState, CorrelationGroup


def _make_group(priority: int = 3) -> CorrelationGroup:
    return CorrelationGroup(
        id="cg-test",
        priority=priority,
        summary="test",
        services=["svc"],
        signals=[],
        topology=None,
        change_candidates=[],
        first_seen="2026-01-01T00:00:00Z",
        last_updated="2026-01-01T00:00:00Z",
        event_count=1,
    )


class TestStateMachine:
    def test_initial_state_is_watching(self):
        sm = StateMachine()
        assert sm.state == AgentState.WATCHING

    def test_p0_group_transitions_to_alert(self):
        sm = StateMachine()
        sm.update([_make_group(priority=0)])
        assert sm.state == AgentState.ALERT

    def test_multiple_p1_transitions_to_alert(self):
        sm = StateMachine()
        sm.update([_make_group(priority=1), _make_group(priority=1)])
        assert sm.state == AgentState.ALERT

    def test_single_p1_stays_watching(self):
        sm = StateMachine()
        sm.update([_make_group(priority=1)])
        assert sm.state == AgentState.WATCHING

    def test_p3_groups_stay_watching(self):
        sm = StateMachine()
        sm.update([_make_group(priority=3)])
        assert sm.state == AgentState.WATCHING

    def test_declare_incident(self):
        sm = StateMachine()
        sm.declare_incident()
        assert sm.state == AgentState.INCIDENT

    def test_resolve_incident(self):
        sm = StateMachine()
        sm.declare_incident()
        sm.resolve_incident()
        assert sm.state == AgentState.WATCHING

    def test_model_failure_transitions_to_degraded(self):
        sm = StateMachine()
        sm.update([], model_healthy=False)
        assert sm.state == AgentState.DEGRADED

    def test_model_recovery_transitions_to_watching(self):
        sm = StateMachine()
        sm.update([], model_healthy=False)
        assert sm.state == AgentState.DEGRADED
        sm.update([], model_healthy=True)
        assert sm.state == AgentState.WATCHING

    def test_model_failure_during_incident_stays_incident(self):
        sm = StateMachine()
        sm.declare_incident()
        sm.update([], model_healthy=False)
        assert sm.state == AgentState.INCIDENT  # INCIDENT overrides DEGRADED

    def test_get_interval_watching(self):
        sm = StateMachine()
        assert sm.get_interval() == 300

    def test_get_interval_alert(self):
        sm = StateMachine()
        sm.update([_make_group(priority=0)])
        assert sm.get_interval() == 60

    def test_get_interval_incident(self):
        sm = StateMachine()
        sm.declare_incident()
        assert sm.get_interval() == 30

    def test_get_interval_degraded(self):
        sm = StateMachine()
        sm.update([], model_healthy=False)
        assert sm.get_interval() == 120

    def test_get_cache_ttl_watching(self):
        sm = StateMachine()
        assert sm.get_cache_ttl() == 900

    def test_get_cache_ttl_incident_is_none(self):
        sm = StateMachine()
        sm.declare_incident()
        assert sm.get_cache_ttl() is None

    def test_alert_back_to_watching_needs_10_min_calm(self):
        sm = StateMachine()
        sm.update([_make_group(priority=0)])  # → ALERT
        assert sm.state == AgentState.ALERT
        # Still P3 groups but no P0/P1
        sm.update([_make_group(priority=3)])
        assert sm.state == AgentState.ALERT  # not 10 min yet
        # Manually advance the calm timer
        from datetime import timedelta
        sm._calm_since = sm._calm_since - timedelta(minutes=11)
        sm.update([_make_group(priority=3)])
        assert sm.state == AgentState.WATCHING


class TestLoadConfig:
    def test_default_config(self):
        from nthlayer_workers.correlate.config import SitRepConfig
        config = SitRepConfig()
        assert config.store_path == "sitrep-events.db"
        assert config.ingestion_port == 8081
        assert config.watching_interval == 300

    def test_load_from_yaml(self, tmp_path):
        from nthlayer_workers.correlate.config import load_config
        config_file = tmp_path / "test-sitrep.yaml"
        config_file.write_text("""
store:
  path: custom-events.db
ingestion:
  port: 9090
state:
  watching_interval_seconds: 600
""")
        config = load_config(str(config_file))
        assert config.store_path == "custom-events.db"
        assert config.ingestion_port == 9090
        assert config.watching_interval == 600

    def test_load_missing_file_returns_defaults(self):
        from nthlayer_workers.correlate.config import load_config
        config = load_config(None)
        assert config.store_path == "sitrep-events.db"
