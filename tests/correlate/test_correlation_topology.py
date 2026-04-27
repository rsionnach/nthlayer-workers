"""Tests for topology-aware grouping."""
from __future__ import annotations

from nthlayer_workers.correlate.correlation.topology import group_topology
from nthlayer_workers.correlate.types import EventType, SitRepEvent, TemporalGroup


def _make_event(service="api", ts="2026-03-01T14:00:00Z", severity=0.5, etype=EventType.ALERT):
    return SitRepEvent(
        id=f"evt-{ts}-{service}",
        timestamp=ts,
        source="prometheus",
        type=etype,
        service=service,
        environment="production",
        severity=severity,
        payload={"alert_name": "test"},
    )


def _make_group(service, severity=0.5, etype=EventType.ALERT):
    evt = _make_event(service=service, severity=severity, etype=etype)
    return TemporalGroup(
        service=service,
        time_window=("2026-03-01T14:00:00Z", "2026-03-01T14:05:00Z"),
        events=[evt],
        count=1,
        peak_severity=severity,
        duration_seconds=300.0,
    )


class TestGroupTopology:
    def test_dependent_services_linked(self):
        """Two services with dependency, both have alerts -> linked TopologyCorrelation."""
        topology = {
            "payment-api": {
                "tier": "critical",
                "dependencies": ["database-primary"],
                "dependents": ["checkout-service"],
            },
            "checkout-service": {
                "tier": "critical",
                "dependencies": ["payment-api"],
                "dependents": [],
            },
        }
        groups = [
            _make_group("payment-api", severity=0.8),
            _make_group("checkout-service", severity=0.6),
        ]
        result = group_topology(groups, topology)
        assert len(result) >= 1
        # Should link payment-api and checkout-service
        all_services = set()
        for tc in result:
            all_services.add(tc.primary_service)
            for rs in tc.related_services:
                all_services.add(rs["service"])
        assert "payment-api" in all_services
        assert "checkout-service" in all_services

    def test_unrelated_services_no_link(self):
        """Two unrelated services -> no topology link."""
        topology = {
            "auth-service": {
                "tier": "critical",
                "dependencies": [],
                "dependents": [],
            },
            "analytics-worker": {
                "tier": "low",
                "dependencies": [],
                "dependents": [],
            },
        }
        groups = [
            _make_group("auth-service", severity=0.8),
            _make_group("analytics-worker", severity=0.5),
        ]
        result = group_topology(groups, topology)
        assert len(result) == 0

    def test_no_topology_data(self):
        """No topology data -> empty list."""
        groups = [_make_group("payment-api"), _make_group("checkout-service")]
        result = group_topology(groups, None)
        assert result == []

    def test_dependency_direction_correct(self):
        """Dependency direction is correct (depends_on vs depended_by)."""
        topology = {
            "checkout-service": {
                "tier": "critical",
                "dependencies": ["payment-api"],
                "dependents": [],
            },
            "payment-api": {
                "tier": "critical",
                "dependencies": [],
                "dependents": ["checkout-service"],
            },
        }
        groups = [
            _make_group("checkout-service", severity=0.8),
            _make_group("payment-api", severity=0.6),
        ]
        result = group_topology(groups, topology)
        assert len(result) >= 1
        tc = result[0]
        # The primary should be the higher-severity service
        # checkout-service has severity 0.8, payment-api has 0.6
        # checkout-service depends_on payment-api
        # Check that related_services has correct relationship directions
        for rs in tc.related_services:
            if rs["service"] == "payment-api" and tc.primary_service == "checkout-service":
                assert rs["relationship"] == "depends_on"
            elif rs["service"] == "checkout-service" and tc.primary_service == "payment-api":
                assert rs["relationship"] == "depended_by"

    def test_single_group_no_correlation(self):
        """Single temporal group cannot form a topology correlation."""
        topology = {
            "payment-api": {
                "tier": "critical",
                "dependencies": ["database-primary"],
                "dependents": [],
            },
        }
        groups = [_make_group("payment-api")]
        result = group_topology(groups, topology)
        assert len(result) == 0

    def test_topology_path_populated(self):
        """Topology path should contain the connected services."""
        topology = {
            "payment-api": {
                "tier": "critical",
                "dependencies": ["database-primary"],
                "dependents": ["checkout-service"],
            },
            "checkout-service": {
                "tier": "critical",
                "dependencies": ["payment-api"],
                "dependents": [],
            },
        }
        groups = [
            _make_group("payment-api", severity=0.8),
            _make_group("checkout-service", severity=0.6),
        ]
        result = group_topology(groups, topology)
        assert len(result) >= 1
        tc = result[0]
        assert len(tc.topology_path) >= 2
