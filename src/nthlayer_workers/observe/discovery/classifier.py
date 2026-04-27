"""Metric classifier for technology and type detection."""

from __future__ import annotations

import re

from nthlayer_workers.observe.discovery.models import DiscoveredMetric, MetricType, TechnologyGroup


class MetricClassifier:
    """Classifies metrics by technology using pattern matching."""

    TECHNOLOGY_PATTERNS = [
        (r"^pg_", TechnologyGroup.POSTGRESQL),
        (r"postgres", TechnologyGroup.POSTGRESQL),
        (r"^redis_", TechnologyGroup.REDIS),
        (r"cache_hits", TechnologyGroup.REDIS),
        (r"cache_misses", TechnologyGroup.REDIS),
        (r"^mongodb_", TechnologyGroup.MONGODB),
        (r"^mongo_", TechnologyGroup.MONGODB),
        (r"^kafka_", TechnologyGroup.KAFKA),
        (r"^mysql_", TechnologyGroup.MYSQL),
        (r"^rabbitmq_", TechnologyGroup.RABBITMQ),
        (r"^kube_", TechnologyGroup.KUBERNETES),
        (r"^container_", TechnologyGroup.KUBERNETES),
        (r"_pod_", TechnologyGroup.KUBERNETES),
        (r"^ecs_", TechnologyGroup.KUBERNETES),
        (r"^http_", TechnologyGroup.HTTP),
        (r"_request", TechnologyGroup.HTTP),
        (r"_response", TechnologyGroup.HTTP),
    ]

    TYPE_PATTERNS = [
        (r"_total$", MetricType.COUNTER),
        (r"_count$", MetricType.COUNTER),
        (r"_created$", MetricType.COUNTER),
        (r"_bucket$", MetricType.HISTOGRAM),
        (r"_sum$", MetricType.SUMMARY),
        (r"_seconds_", MetricType.HISTOGRAM),
        (r"_bytes", MetricType.GAUGE),
        (r"_ratio$", MetricType.GAUGE),
        (r"_percentage$", MetricType.GAUGE),
    ]

    def classify(self, metric: DiscoveredMetric) -> DiscoveredMetric:
        """Classify a discovered metric by technology and type."""
        metric.technology = self._classify_technology(metric.name)

        if metric.type == MetricType.UNKNOWN:
            metric.type = self._infer_type(metric.name)

        return metric

    def _classify_technology(self, metric_name: str) -> TechnologyGroup:
        metric_lower = metric_name.lower()
        for pattern, technology in self.TECHNOLOGY_PATTERNS:
            if re.search(pattern, metric_lower):
                return technology
        return TechnologyGroup.CUSTOM

    def _infer_type(self, metric_name: str) -> MetricType:
        metric_lower = metric_name.lower()
        for pattern, metric_type in self.TYPE_PATTERNS:
            if re.search(pattern, metric_lower):
                return metric_type
        return MetricType.GAUGE
