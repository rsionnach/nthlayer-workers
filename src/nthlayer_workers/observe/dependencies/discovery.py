"""Dependency discovery orchestrator."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from nthlayer_common.dependency_models import (
    BlastRadiusResult,
    DependencyGraph,
    DependencyType,
    DiscoveredDependency,
    ResolvedDependency,
)
from nthlayer_common.errors import ProviderError
from nthlayer_common.identity import IdentityResolver, ServiceIdentity

from nthlayer_workers.observe.dependencies.providers.base import BaseDepProvider, ProviderHealth


class DiscoveryError(ProviderError):
    """Raised when dependency discovery encounters an error."""


@dataclass
class DependencyDiscoveryResult:
    """Result of dependency discovery for a service."""

    service: str
    upstream: list[ResolvedDependency] = field(default_factory=list)
    downstream: list[ResolvedDependency] = field(default_factory=list)
    providers_queried: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def total_dependencies(self) -> int:
        return len(self.upstream) + len(self.downstream)


@dataclass
class DependencyDiscovery:
    """Orchestrate dependency discovery across multiple providers."""

    providers: list[BaseDepProvider] = field(default_factory=list)
    resolver: IdentityResolver = field(default_factory=IdentityResolver)
    tier_mapping: dict[str, str] = field(default_factory=dict)

    def add_provider(self, provider: BaseDepProvider) -> None:
        self.providers.append(provider)

    def set_tier(self, service: str, tier: str) -> None:
        self.tier_mapping[service] = tier

    async def health_check(self) -> dict[str, ProviderHealth]:
        results: dict[str, ProviderHealth] = {}

        async def check_provider(provider: BaseDepProvider) -> tuple[str, ProviderHealth]:
            try:
                health = await provider.health_check()
                return (provider.name, health)
            except Exception as e:
                return (provider.name, ProviderHealth(healthy=False, message=str(e)))

        tasks = [check_provider(p) for p in self.providers]
        for name, health in await asyncio.gather(*tasks):
            results[name] = health

        return results

    async def discover(self, service: str) -> DependencyDiscoveryResult:
        result = DependencyDiscoveryResult(service=service)

        upstream_tasks = []
        downstream_tasks = []

        for provider in self.providers:
            upstream_tasks.append(self._discover_upstream(provider, service))
            downstream_tasks.append(self._discover_downstream(provider, service))

        upstream_results = await asyncio.gather(*upstream_tasks, return_exceptions=True)
        for provider, deps_or_error in zip(self.providers, upstream_results, strict=True):
            result.providers_queried.append(provider.name)
            if isinstance(deps_or_error, BaseException):
                result.errors[provider.name] = str(deps_or_error)
            elif isinstance(deps_or_error, list):
                result.upstream.extend(deps_or_error)

        downstream_results = await asyncio.gather(*downstream_tasks, return_exceptions=True)
        for provider, deps_or_error in zip(self.providers, downstream_results, strict=True):
            if isinstance(deps_or_error, BaseException):
                if provider.name not in result.errors:
                    result.errors[provider.name] = str(deps_or_error)
            elif isinstance(deps_or_error, list):
                result.downstream.extend(deps_or_error)

        result.upstream = self._deduplicate(result.upstream)
        result.downstream = self._deduplicate(result.downstream)

        return result

    async def _discover_upstream(
        self, provider: BaseDepProvider, service: str
    ) -> list[ResolvedDependency]:
        discovered = await provider.discover(service)
        return self._resolve_dependencies(discovered)

    async def _discover_downstream(
        self, provider: BaseDepProvider, service: str
    ) -> list[ResolvedDependency]:
        if hasattr(provider, "discover_downstream"):
            discovered = await provider.discover_downstream(service)
            return self._resolve_dependencies(discovered)
        return []

    def _resolve_dependencies(
        self, discovered: list[DiscoveredDependency]
    ) -> list[ResolvedDependency]:
        resolved: list[ResolvedDependency] = []

        for dep in discovered:
            source_match = self.resolver.resolve(dep.source_service)
            target_match = self.resolver.resolve(dep.target_service)

            source_identity = (
                source_match.identity
                if source_match and source_match.identity is not None
                else ServiceIdentity(canonical_name=dep.source_service)
            )
            target_identity = (
                target_match.identity
                if target_match and target_match.identity is not None
                else ServiceIdentity(canonical_name=dep.target_service)
            )

            resolved.append(
                ResolvedDependency(
                    source=source_identity,
                    target=target_identity,
                    dep_type=dep.dep_type,
                    confidence=dep.confidence,
                    providers=[dep.provider],
                    metadata=dep.metadata,
                )
            )

        return resolved

    def _deduplicate(self, deps: list[ResolvedDependency]) -> list[ResolvedDependency]:
        seen: dict[str, ResolvedDependency] = {}

        for dep in deps:
            key = f"{dep.source.canonical_name}:{dep.target.canonical_name}:{dep.dep_type.value}"

            if key not in seen:
                seen[key] = dep
            else:
                existing = seen[key]
                existing.providers = list(set(existing.providers + dep.providers))
                existing.confidence = max(existing.confidence, dep.confidence)
                existing.metadata.update(dep.metadata)

        return list(seen.values())

    async def build_graph(self, services: list[str] | None = None) -> DependencyGraph:
        graph = DependencyGraph()
        graph.providers_used = [p.name for p in self.providers]

        if services is None:
            services = await self._list_all_services()

        for service in services:
            result = await self.discover(service)
            identity = ServiceIdentity(canonical_name=service)
            graph.add_service(identity)

            for dep in result.upstream:
                graph.add_edge(dep)
            for dep in result.downstream:
                graph.add_edge(dep)

        return graph

    async def _list_all_services(self) -> list[str]:
        all_services: set[str] = set()

        for provider in self.providers:
            try:
                services = await provider.list_services()
                for service in services:
                    match = self.resolver.resolve(service)
                    if match and match.identity is not None:
                        all_services.add(match.identity.canonical_name)
                    else:
                        all_services.add(service)
            except Exception:
                continue

        return sorted(all_services)

    def calculate_blast_radius(
        self, service: str, graph: DependencyGraph, max_depth: int = 10
    ) -> BlastRadiusResult:
        result = BlastRadiusResult(
            service=service,
            tier=self.tier_mapping.get(service),
        )

        result.direct_downstream = graph.get_downstream(service)
        result.transitive_downstream = graph.get_transitive_downstream(service, max_depth=max_depth)

        affected_services: set[str] = set()
        critical_services: set[str] = set()

        for dep in result.direct_downstream:
            affected_services.add(dep.source.canonical_name)
            if self.tier_mapping.get(dep.source.canonical_name) == "critical":
                critical_services.add(dep.source.canonical_name)

        for dep, _depth in result.transitive_downstream:
            affected_services.add(dep.source.canonical_name)
            if self.tier_mapping.get(dep.source.canonical_name) == "critical":
                critical_services.add(dep.source.canonical_name)

        result.total_services_affected = len(affected_services)
        result.critical_services_affected = len(critical_services)

        result.risk_level = self._calculate_risk_level(
            total_affected=result.total_services_affected,
            critical_affected=result.critical_services_affected,
            service_tier=result.tier,
        )
        result.recommendation = self._generate_recommendation(result)

        return result

    def _calculate_risk_level(
        self, total_affected: int, critical_affected: int, service_tier: str | None
    ) -> str:
        if critical_affected >= 2 or total_affected >= 10:
            return "critical"
        if total_affected >= 6:
            return "high"
        if total_affected >= 3 or critical_affected >= 1:
            return "medium"
        return "low"

    def _generate_recommendation(self, result: BlastRadiusResult) -> str:
        recommendations = {
            "critical": "High-impact deployment. Requires change advisory board approval and deployment during maintenance window with full rollback plan.",
            "high": "Significant impact. Schedule during low-traffic window with staged rollout and monitoring.",
            "medium": "Moderate impact. Consider canary deployment and monitor dependent services closely.",
            "low": "Low impact. Standard deployment process is appropriate.",
        }
        return recommendations.get(result.risk_level, recommendations["low"])


def create_demo_discovery() -> tuple[DependencyDiscovery, DependencyGraph]:
    """Create a demo discovery instance with sample data."""
    discovery = DependencyDiscovery()

    payment_api = ServiceIdentity(canonical_name="payment-api")
    user_service = ServiceIdentity(canonical_name="user-service")
    checkout_api = ServiceIdentity(canonical_name="checkout-api")
    order_service = ServiceIdentity(canonical_name="order-service")
    mobile_gateway = ServiceIdentity(canonical_name="mobile-gateway")
    notification = ServiceIdentity(canonical_name="notification-service")
    postgresql = ServiceIdentity(canonical_name="postgresql")
    redis = ServiceIdentity(canonical_name="redis")

    for identity in [payment_api, user_service, checkout_api, order_service, mobile_gateway, notification, postgresql, redis]:
        discovery.resolver.register(identity)

    discovery.set_tier("payment-api", "critical")
    discovery.set_tier("checkout-api", "critical")
    discovery.set_tier("order-service", "critical")
    discovery.set_tier("user-service", "standard")
    discovery.set_tier("mobile-gateway", "standard")
    discovery.set_tier("notification-service", "standard")

    graph = DependencyGraph()
    graph.providers_used = ["prometheus"]

    for identity in [payment_api, user_service, checkout_api, order_service, mobile_gateway, notification, postgresql, redis]:
        graph.add_service(identity)

    graph.add_edge(ResolvedDependency(source=payment_api, target=user_service, dep_type=DependencyType.SERVICE, confidence=0.95, providers=["prometheus"]))
    graph.add_edge(ResolvedDependency(source=payment_api, target=postgresql, dep_type=DependencyType.DATASTORE, confidence=0.90, providers=["prometheus"]))
    graph.add_edge(ResolvedDependency(source=payment_api, target=redis, dep_type=DependencyType.DATASTORE, confidence=0.85, providers=["prometheus"]))
    graph.add_edge(ResolvedDependency(source=checkout_api, target=payment_api, dep_type=DependencyType.SERVICE, confidence=0.95, providers=["prometheus"]))
    graph.add_edge(ResolvedDependency(source=mobile_gateway, target=payment_api, dep_type=DependencyType.SERVICE, confidence=0.90, providers=["prometheus"]))
    graph.add_edge(ResolvedDependency(source=order_service, target=checkout_api, dep_type=DependencyType.SERVICE, confidence=0.92, providers=["prometheus"]))
    graph.add_edge(ResolvedDependency(source=notification, target=checkout_api, dep_type=DependencyType.SERVICE, confidence=0.88, providers=["prometheus"]))

    return discovery, graph
