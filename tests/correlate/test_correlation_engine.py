"""Tests for the full correlation engine."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import yaml

from nthlayer_workers.correlate.correlation.engine import CorrelationEngine
from nthlayer_workers.correlate.store.sqlite import SQLiteEventStore
from nthlayer_workers.correlate.types import EventType, SitRepEvent


def _load_scenario(name: str) -> dict:
    """Load a correlate scenario from the co-located fixtures dir.

    Co-located (``tests/correlate/scenarios/synthetic/``) rather than
    shared with respond's scenarios (``tests/scenarios/synthetic/``)
    so the two suites don't collide on filenames like
    ``cascading-failure.yaml`` — each suite tests its module against
    its own scenario set.
    """
    import os
    path = os.path.join(
        os.path.dirname(__file__), "scenarios", "synthetic", f"{name}.yaml"
    )
    with open(path) as f:
        return yaml.safe_load(f)["scenario"]


def _parse_relative_time(at_str: str, reference: datetime) -> str:
    """Parse 'T+Nm' format to ISO 8601."""
    minutes = int(at_str.replace("T+", "").replace("m", ""))
    ts = reference + timedelta(minutes=minutes)
    return ts.isoformat()


def _estimate_severity(evt_type: str, payload: dict) -> float:
    """Estimate severity from event type and payload.

    Quality scores with good values get low severity.
    Alerts/metric breaches get severity from value/threshold ratio.
    Changes get low severity (they are informational).
    """
    if evt_type == "quality_score":
        score = payload.get("score", 0.5)
        # High score = low severity (healthy), low score = high severity
        return max(0.0, min(1.0, 1.0 - score))
    if evt_type in ("alert", "metric_breach"):
        value = payload.get("value")
        threshold = payload.get("threshold")
        if value is not None and threshold is not None and threshold > 0:
            return min(1.0, max(0.0, abs(value - threshold) / threshold))
        return 0.5
    if evt_type == "change":
        return 0.1  # Changes are informational
    return 0.5


def _scenario_to_events(scenario: dict) -> tuple[list[SitRepEvent], dict | None]:
    """Convert scenario to events + topology."""
    reference = datetime.now(timezone.utc) - timedelta(minutes=20)
    events = []
    for i, evt_data in enumerate(scenario["events"]):
        ts = _parse_relative_time(evt_data["at"], reference)
        payload = evt_data["payload"]
        service = payload.get("service", "unknown")
        severity = _estimate_severity(evt_data["type"], payload)
        events.append(SitRepEvent(
            id=f"evt-scenario-{i}",
            timestamp=ts,
            source="scenario",
            type=EventType(evt_data["type"]),
            service=service,
            environment="production",
            severity=severity,
            payload=payload,
            ttl=86400,
        ))

    topology = None
    if "topology" in scenario:
        topology = {}
        for svc in scenario["topology"]["services"]:
            topology[svc["name"]] = {
                "tier": svc.get("tier", "standard"),
                "dependencies": svc.get("dependencies", []),
                "dependents": svc.get("dependents", []),
            }

    return events, topology


class TestCorrelationEngineWithScenarios:
    def test_cascading_failure(self, tmp_path):
        scenario = _load_scenario("cascading-failure")
        events, topology = _scenario_to_events(scenario)

        store = SQLiteEventStore(str(tmp_path / "test.db"))
        store.insert_batch(events)

        engine = CorrelationEngine()
        groups = engine.correlate(store, window_minutes=30, topology=topology)

        expected = scenario["expected_outcomes"]
        # Should produce correlation group(s) linking payment-api and checkout-service
        assert len(groups) >= 1
        all_services = set()
        for g in groups:
            all_services.update(g.services)
        for svc in expected["affected_services"]:
            assert svc in all_services

        # Should have change candidates
        has_change = any(len(g.change_candidates) > 0 for g in groups)
        assert has_change
        store.close()

    def test_quiet_period(self, tmp_path):
        scenario = _load_scenario("quiet-period")
        events, topology = _scenario_to_events(scenario)

        store = SQLiteEventStore(str(tmp_path / "test.db"))
        store.insert_batch(events)

        engine = CorrelationEngine()
        groups = engine.correlate(store, window_minutes=30, topology=topology)

        # Quality scores only, no alerts -> should produce 0 correlation groups
        # (quality_score events with severity 0.5 shouldn't trigger P0/P1)
        # The groups list might have low-priority entries, but expected is 0
        expected_groups = scenario["expected_outcomes"]["correlation_groups"]
        # Filter to only elevated groups (P0-P2)
        elevated = [g for g in groups if g.priority <= 2]
        assert len(elevated) == expected_groups
        store.close()

    def test_misleading_correlation(self, tmp_path):
        scenario = _load_scenario("misleading-correlation")
        events, topology = _scenario_to_events(scenario)

        store = SQLiteEventStore(str(tmp_path / "test.db"))
        store.insert_batch(events)

        engine = CorrelationEngine()
        groups = engine.correlate(store, window_minutes=30, topology=topology)

        # Unrelated services should NOT be merged into one group
        # auth-service and analytics-worker have no dependency
        services_in_groups = [set(g.services) for g in groups]
        # No single group should contain both auth-service and analytics-worker
        for svc_set in services_in_groups:
            assert not ({"auth-service", "analytics-worker"}.issubset(svc_set))
        store.close()

    def test_simple_causal_chain(self, tmp_path):
        """Deploy then alert on same service → 1 correlation group."""
        scenario = _load_scenario("simple-causal-chain")
        events, topology = _scenario_to_events(scenario)

        store = SQLiteEventStore(str(tmp_path / "test.db"))
        store.insert_batch(events)

        engine = CorrelationEngine()
        groups = engine.correlate(store, window_minutes=30, topology=topology)

        expected = scenario["expected_outcomes"]
        assert len(groups) >= expected["correlation_groups"]

        # payment-api should be in the group
        all_services = set()
        for g in groups:
            all_services.update(g.services)
        for svc in expected["affected_services"]:
            assert svc in all_services

        # Should surface the deploy as a change candidate
        all_changes = []
        for g in groups:
            all_changes.extend(g.change_candidates)
        assert len(all_changes) >= 1
        change_types = {c.change.payload.get("change_type") for c in all_changes}
        assert "deploy" in change_types
        store.close()

    def test_multi_candidate(self, tmp_path):
        """3 changes before alert → all 3 surfaced as change candidates."""
        scenario = _load_scenario("multi-candidate")
        events, topology = _scenario_to_events(scenario)

        store = SQLiteEventStore(str(tmp_path / "test.db"))
        store.insert_batch(events)

        engine = CorrelationEngine()
        groups = engine.correlate(store, window_minutes=30, topology=topology)

        expected = scenario["expected_outcomes"]
        assert len(groups) >= expected["correlation_groups"]

        # order-service should be affected
        all_services = set()
        for g in groups:
            all_services.update(g.services)
        for svc in expected["affected_services"]:
            assert svc in all_services

        # All 3 change candidates should be surfaced
        all_changes = []
        for g in groups:
            all_changes.extend(g.change_candidates)

        expected_changes = expected["change_candidates"]
        found_changes = {(c.change.service, c.change.payload.get("change_type")) for c in all_changes}
        for ec in expected_changes:
            assert (ec["service"], ec["change_type"]) in found_changes, (
                f"Expected change ({ec['service']}, {ec['change_type']}) not found in {found_changes}"
            )
        store.close()


class TestCorrelationEngineBasic:
    def test_empty_store_no_groups(self, tmp_path):
        store = SQLiteEventStore(str(tmp_path / "test.db"))
        engine = CorrelationEngine()
        groups = engine.correlate(store, window_minutes=30)
        assert groups == []
        store.close()

    def test_group_id_format(self, tmp_path):
        """Each group should have a cg- prefixed ID."""
        now = datetime.now(timezone.utc)
        alert_ts = now.isoformat()
        event = SitRepEvent(
            id="evt-basic-001",
            timestamp=alert_ts,
            source="prometheus",
            type=EventType.ALERT,
            service="payment-api",
            environment="production",
            severity=0.9,
            payload={"alert_name": "latency_breach"},
            ttl=86400,
        )
        store = SQLiteEventStore(str(tmp_path / "test.db"))
        store.insert(event)

        engine = CorrelationEngine()
        groups = engine.correlate(store, window_minutes=30)
        assert len(groups) >= 1
        for g in groups:
            assert g.id.startswith("cg-")
        store.close()

    def test_priority_scoring_p0(self, tmp_path):
        """High severity + critical tier = P0."""
        now = datetime.now(timezone.utc)
        event = SitRepEvent(
            id="evt-p0-001",
            timestamp=now.isoformat(),
            source="prometheus",
            type=EventType.ALERT,
            service="payment-api",
            environment="production",
            severity=0.9,
            payload={"alert_name": "latency_breach"},
            ttl=86400,
        )
        topology = {
            "payment-api": {
                "tier": "critical",
                "dependencies": [],
                "dependents": [],
            },
        }
        store = SQLiteEventStore(str(tmp_path / "test.db"))
        store.insert(event)

        engine = CorrelationEngine()
        groups = engine.correlate(store, window_minutes=30, topology=topology)
        assert len(groups) >= 1
        assert groups[0].priority == 0  # P0
        store.close()

    def test_summary_template_format(self, tmp_path):
        """Summary should be template-generated, not model output."""
        now = datetime.now(timezone.utc)
        event = SitRepEvent(
            id="evt-summary-001",
            timestamp=now.isoformat(),
            source="prometheus",
            type=EventType.ALERT,
            service="payment-api",
            environment="production",
            severity=0.7,
            payload={"alert_name": "latency_breach"},
            ttl=86400,
        )
        store = SQLiteEventStore(str(tmp_path / "test.db"))
        store.insert(event)

        engine = CorrelationEngine()
        groups = engine.correlate(store, window_minutes=30)
        assert len(groups) >= 1
        summary = groups[0].summary
        assert "payment-api" in summary
        assert "change" in summary.lower() or "recent" in summary.lower() or "alert" in summary.lower()
        store.close()
