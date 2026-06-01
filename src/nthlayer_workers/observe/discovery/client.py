"""Prometheus metric discovery client."""

from __future__ import annotations

import re

import httpx
import structlog

from nthlayer_workers.observe.discovery.classifier import MetricClassifier
from nthlayer_workers.observe.discovery.models import (
    DiscoveredMetric,
    DiscoveryResult,
    MetricType,
    TechnologyGroup,
)

logger = structlog.get_logger()


class MetricDiscoveryClient:
    """Client for discovering metrics from Prometheus."""

    def __init__(
        self,
        prometheus_url: str,
        username: str | None = None,
        password: str | None = None,
        bearer_token: str | None = None,
    ):
        self.prometheus_url = prometheus_url.rstrip("/")
        self.auth = (username, password) if username and password else None
        self.headers = {"Authorization": f"Bearer {bearer_token}"} if bearer_token else {}
        self.classifier = MetricClassifier()

    def discover(self, selector: str) -> DiscoveryResult:
        """Discover all metrics matching the given selector."""
        metric_names = self._get_metric_names(selector)

        metrics = []
        for name in metric_names:
            metric = self._discover_metric(name, selector)
            if metric:
                metrics.append(metric)

        classified_metrics = [self.classifier.classify(m) for m in metrics]

        result = DiscoveryResult(
            service=self._extract_service_from_selector(selector),
            total_metrics=len(classified_metrics),
            metrics=classified_metrics,
        )

        for metric in classified_metrics:
            tech = metric.technology.value if isinstance(metric.technology, TechnologyGroup) else metric.technology
            if tech not in result.metrics_by_technology:
                result.metrics_by_technology[tech] = []
            result.metrics_by_technology[tech].append(metric)

        for metric in classified_metrics:
            mtype = metric.type.value if isinstance(metric.type, MetricType) else metric.type
            if mtype not in result.metrics_by_type:
                result.metrics_by_type[mtype] = []
            result.metrics_by_type[mtype].append(metric)

        return result

    def _get_metric_names(self, selector: str) -> list[str]:
        """Query Prometheus for all metric names matching selector."""
        url = f"{self.prometheus_url}/api/v1/series"
        params = {"match[]": selector}

        if "/metrics" in self.prometheus_url or "fly.dev" in self.prometheus_url:
            return self._get_metrics_from_endpoint(selector)

        try:
            response = httpx.get(
                url, params=params, auth=self.auth, headers=self.headers, timeout=30
            )
            response.raise_for_status()
            data = response.json()

            if data.get("status") != "success":
                return []

            metric_names = set()
            for series in data.get("data", []):
                if "__name__" in series:
                    metric_names.add(series["__name__"])

            return sorted(metric_names)

        except Exception as e:
            logger.warning("prometheus_series_query_failed", error=str(e))
            return []

    def _discover_metric(self, metric_name: str, selector: str) -> DiscoveredMetric | None:
        """Discover detailed information about a specific metric."""
        metadata = self._get_metric_metadata(metric_name)
        labels = self._get_label_values(metric_name, selector)

        return DiscoveredMetric(
            name=metric_name,
            type=MetricType(metadata.get("type", "unknown")),
            technology=TechnologyGroup.UNKNOWN,
            help_text=metadata.get("help"),
            labels=labels,
        )

    def _get_metric_metadata(self, metric_name: str) -> dict:
        """Query Prometheus metadata API for metric type and help text."""
        url = f"{self.prometheus_url}/api/v1/metadata"
        params = {"metric": metric_name}

        try:
            response = httpx.get(
                url, params=params, auth=self.auth, headers=self.headers, timeout=10
            )
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "success":
                metric_data = data.get("data", {}).get(metric_name, [])
                if metric_data:
                    return metric_data[0]

            return {}

        except Exception:
            return {}

    def _get_label_values(self, metric_name: str, selector: str) -> dict[str, list[str]]:
        """Get all label values for a metric."""
        url = f"{self.prometheus_url}/api/v1/series"
        full_selector = f"{metric_name}{selector}"
        params = {"match[]": full_selector}

        try:
            response = httpx.get(
                url, params=params, auth=self.auth, headers=self.headers, timeout=10
            )
            response.raise_for_status()
            data = response.json()

            if data.get("status") != "success":
                return {}

            labels: dict[str, set] = {}
            for series in data.get("data", []):
                for label, value in series.items():
                    if label == "__name__":
                        continue
                    if label not in labels:
                        labels[label] = set()
                    labels[label].add(value)

            return {k: sorted(v) for k, v in labels.items()}

        except Exception:
            return {}

    def _get_metrics_from_endpoint(self, selector: str) -> list[str]:
        """Parse /metrics endpoint directly (fallback for non-Prometheus targets)."""
        service = self._extract_service_from_selector(selector)
        url = f"{self.prometheus_url}/metrics"
        filter_by_service = service and service != "unknown"

        try:
            response = httpx.get(url, auth=self.auth, headers=self.headers, timeout=30)
            response.raise_for_status()

            metric_names = set()
            for line in response.text.split("\n"):
                if not line or line.startswith("#"):
                    continue

                if filter_by_service and f'service="{service}"' not in line:
                    continue

                if "{" in line:
                    metric_names.add(line.split("{")[0])
                elif " " in line:
                    metric_names.add(line.split(" ")[0])

            return sorted(metric_names)

        except Exception as e:
            logger.warning("metrics_endpoint_parse_failed", error=str(e))
            return []

    def _extract_service_from_selector(self, selector: str) -> str:
        """Extract service name from selector string."""
        match = re.search(r'service="([^"]+)"', selector)
        return match.group(1) if match else "unknown"
