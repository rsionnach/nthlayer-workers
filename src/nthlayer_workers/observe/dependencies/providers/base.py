"""Base class for dependency discovery providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass

from nthlayer_common.dependency_models import DependencyType, DiscoveredDependency


def deduplicate_dependencies(deps: list[DiscoveredDependency]) -> list[DiscoveredDependency]:
    """Remove duplicate dependencies, keeping highest confidence."""
    seen: dict[str, DiscoveredDependency] = {}

    for dep in deps:
        key = f"{dep.source_service}:{dep.target_service}:{dep.dep_type.value}"
        if key not in seen or dep.confidence > seen[key].confidence:
            seen[key] = dep

    return list(seen.values())


def infer_dependency_type(service_name: str) -> DependencyType:
    """Infer dependency type from service name patterns."""
    name_lower = service_name.lower()

    if any(db in name_lower for db in ("postgres", "mysql", "mongo", "redis", "elastic", "cassandra")):
        return DependencyType.DATASTORE

    if any(q in name_lower for q in ("kafka", "rabbitmq", "sqs", "nats", "pulsar")):
        return DependencyType.QUEUE

    return DependencyType.SERVICE


@dataclass
class ProviderHealth:
    """Health status of a provider."""

    healthy: bool
    message: str
    latency_ms: float | None = None


class BaseDepProvider(ABC):
    """Abstract base class for dependency discovery providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider name for identification."""

    @abstractmethod
    async def discover(self, service: str) -> list[DiscoveredDependency]:
        """Discover dependencies for a service."""

    @abstractmethod
    async def list_services(self) -> list[str]:
        """List all services known to this provider."""

    @abstractmethod
    async def health_check(self) -> ProviderHealth:
        """Check provider connectivity and health."""

    async def get_service_attributes(self, service: str) -> dict:
        """Get service attributes for identity correlation."""
        return {}

    async def discover_all(self) -> AsyncIterator[DiscoveredDependency]:
        """Discover all dependencies for all services."""
        services = await self.list_services()
        for service in services:
            deps = await self.discover(service)
            for dep in deps:
                yield dep
