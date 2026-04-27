"""Tests for the dependencies module."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nthlayer_common.dependency_models import (
    DependencyGraph,
    DependencyType,
    DiscoveredDependency,
    ResolvedDependency,
)
from nthlayer_common.identity import ServiceIdentity

from nthlayer_workers.observe.dependencies.discovery import (
    DependencyDiscovery,
    DependencyDiscoveryResult,
    create_demo_discovery,
)
from nthlayer_workers.observe.dependencies.providers.base import (
    BaseDepProvider,
    ProviderHealth,
    deduplicate_dependencies,
    infer_dependency_type,
)


class TestBaseProviderHelpers:
    def test_infer_dependency_type_datastore(self):
        assert infer_dependency_type("postgres-primary") == DependencyType.DATASTORE
        assert infer_dependency_type("redis-cache") == DependencyType.DATASTORE
        assert infer_dependency_type("mongodb-users") == DependencyType.DATASTORE

    def test_infer_dependency_type_queue(self):
        assert infer_dependency_type("kafka-events") == DependencyType.QUEUE
        assert infer_dependency_type("rabbitmq-notifications") == DependencyType.QUEUE

    def test_infer_dependency_type_service(self):
        assert infer_dependency_type("payment-api") == DependencyType.SERVICE
        assert infer_dependency_type("user-service") == DependencyType.SERVICE

    def test_deduplicate_keeps_highest_confidence(self):
        deps = [
            DiscoveredDependency(
                source_service="a", target_service="b",
                dep_type=DependencyType.SERVICE, confidence=0.7, provider="prom"
            ),
            DiscoveredDependency(
                source_service="a", target_service="b",
                dep_type=DependencyType.SERVICE, confidence=0.9, provider="k8s"
            ),
        ]
        result = deduplicate_dependencies(deps)
        assert len(result) == 1
        assert result[0].confidence == 0.9


class TestDependencyDiscoveryResult:
    def test_total_dependencies(self):
        r = DependencyDiscoveryResult(service="svc")
        assert r.total_dependencies == 0

        src = ServiceIdentity(canonical_name="a")
        tgt = ServiceIdentity(canonical_name="b")
        r.upstream.append(
            ResolvedDependency(source=src, target=tgt, dep_type=DependencyType.SERVICE, confidence=0.9, providers=["p"])
        )
        assert r.total_dependencies == 1


class TestDependencyDiscovery:
    def _make_mock_provider(self, name: str, deps: list[DiscoveredDependency] | None = None):
        provider = AsyncMock(spec=BaseDepProvider)
        provider.name = name
        provider.discover = AsyncMock(return_value=deps or [])
        provider.health_check = AsyncMock(return_value=ProviderHealth(healthy=True, message="ok"))
        # Remove discover_downstream to test the hasattr path
        if hasattr(provider, "discover_downstream"):
            del provider.discover_downstream
        return provider

    async def test_discover_empty(self):
        discovery = DependencyDiscovery()
        result = await discovery.discover("svc")
        assert result.total_dependencies == 0
        assert result.providers_queried == []

    async def test_discover_with_provider(self):
        provider = self._make_mock_provider("prom", [
            DiscoveredDependency(
                source_service="payment-api", target_service="postgres",
                dep_type=DependencyType.DATASTORE, confidence=0.9, provider="prom"
            )
        ])
        discovery = DependencyDiscovery(providers=[provider])
        result = await discovery.discover("payment-api")

        assert len(result.upstream) == 1
        assert result.upstream[0].target.canonical_name == "postgres"
        assert "prom" in result.providers_queried

    async def test_discover_handles_provider_error(self):
        provider = AsyncMock(spec=BaseDepProvider)
        provider.name = "failing"
        provider.discover = AsyncMock(side_effect=Exception("connection refused"))
        if hasattr(provider, "discover_downstream"):
            del provider.discover_downstream

        discovery = DependencyDiscovery(providers=[provider])
        result = await discovery.discover("svc")

        assert "failing" in result.errors
        assert result.total_dependencies == 0

    async def test_health_check(self):
        provider = self._make_mock_provider("prom")
        discovery = DependencyDiscovery(providers=[provider])
        health = await discovery.health_check()

        assert "prom" in health
        assert health["prom"].healthy

    def test_calculate_blast_radius_low(self):
        discovery = DependencyDiscovery()
        graph = DependencyGraph()

        src = ServiceIdentity(canonical_name="svc")
        graph.add_service(src)

        result = discovery.calculate_blast_radius("svc", graph)
        assert result.risk_level == "low"
        assert result.total_services_affected == 0

    def test_calculate_blast_radius_with_downstream(self):
        discovery, graph = create_demo_discovery()
        result = discovery.calculate_blast_radius("payment-api", graph)

        assert result.total_services_affected > 0
        assert result.risk_level in ("low", "medium", "high", "critical")
        assert result.recommendation != ""


class TestCreateDemoDiscovery:
    def test_creates_valid_graph(self):
        discovery, graph = create_demo_discovery()
        assert len(graph.services) == 8
        assert len(graph.edges) == 7
        assert "prometheus" in graph.providers_used


class TestDependenciesCLI:
    def test_dependencies_help(self, capsys):
        from nthlayer_workers.observe.cli import main

        with pytest.raises(SystemExit) as exc_info:
            main(["dependencies", "--help"])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "--service" in captured.out
        assert "--prometheus-url" in captured.out

    def test_blast_radius_help(self, capsys):
        from nthlayer_workers.observe.cli import main

        with pytest.raises(SystemExit) as exc_info:
            main(["blast-radius", "--help"])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "--service" in captured.out
