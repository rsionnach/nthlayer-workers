"""Data models for metric discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class MetricType(str, Enum):
    """Prometheus metric types."""

    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"
    SUMMARY = "summary"
    UNKNOWN = "unknown"


class TechnologyGroup(str, Enum):
    """Technology classification for metrics."""

    POSTGRESQL = "postgresql"
    REDIS = "redis"
    MONGODB = "mongodb"
    KAFKA = "kafka"
    MYSQL = "mysql"
    RABBITMQ = "rabbitmq"
    KUBERNETES = "kubernetes"
    HTTP = "http"
    CUSTOM = "custom"
    UNKNOWN = "unknown"


@dataclass
class DiscoveredMetric:
    """A metric discovered from Prometheus."""

    name: str
    type: MetricType = MetricType.UNKNOWN
    technology: TechnologyGroup = TechnologyGroup.UNKNOWN
    help_text: str | None = None
    labels: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class DiscoveryResult:
    """Result of metric discovery for a service."""

    service: str
    total_metrics: int
    metrics: list[DiscoveredMetric] = field(default_factory=list)
    metrics_by_technology: dict[str, list[DiscoveredMetric]] = field(default_factory=dict)
    metrics_by_type: dict[str, list[DiscoveredMetric]] = field(default_factory=dict)
