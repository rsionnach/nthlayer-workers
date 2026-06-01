"""Extract declared metrics from service resources to form the metric contract."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from nthlayer_workers.observe.verification.models import (
    DeclaredMetric,
    MetricContract,
    MetricSource,
)


@dataclass
class Resource:
    """Minimal resource representation for metric extraction.

    Compatible with nthlayer.specs.models.Resource.
    """

    kind: str
    spec: dict[str, Any]
    name: str | None = None


def extract_metric_contract(
    service_name: str,
    resources: list[Resource],
) -> MetricContract:
    """Extract all declared metrics from service resources."""
    contract = MetricContract(service_name=service_name)

    for resource in resources:
        if resource.kind == "SLO":
            contract.metrics.extend(_extract_slo_metrics(resource))
        elif resource.kind == "Observability":
            contract.metrics.extend(_extract_observability_metrics(resource))

    # Deduplicate by metric name (keep first occurrence)
    seen: set[str] = set()
    unique_metrics = []
    for metric in contract.metrics:
        if metric.name not in seen:
            seen.add(metric.name)
            unique_metrics.append(metric)
    contract.metrics = unique_metrics

    return contract


def _extract_slo_metrics(resource: Resource) -> list[DeclaredMetric]:
    """Extract metrics from SLO indicator queries."""
    metrics = []
    spec = resource.spec or {}
    indicators = spec.get("indicators", [])

    for indicator in indicators:
        success_ratio = indicator.get("success_ratio", {})
        for query_key in ["total_query", "good_query", "error_query"]:
            query = success_ratio.get(query_key, "")
            if query:
                for name in _extract_metrics_from_query(query):
                    metrics.append(
                        DeclaredMetric(
                            name=name,
                            source=MetricSource.SLO_INDICATOR,
                            query=query,
                            resource_name=resource.name,
                        )
                    )

        latency_query = indicator.get("latency_query", "")
        if latency_query:
            for name in _extract_metrics_from_query(latency_query):
                metrics.append(
                    DeclaredMetric(
                        name=name,
                        source=MetricSource.SLO_INDICATOR,
                        query=latency_query,
                        resource_name=resource.name,
                    )
                )

    return metrics


def _extract_observability_metrics(resource: Resource) -> list[DeclaredMetric]:
    """Extract metrics from Observability declarations."""
    metrics = []
    spec = resource.spec or {}

    for metric_name in spec.get("metrics", []):
        if isinstance(metric_name, str):
            metrics.append(
                DeclaredMetric(
                    name=metric_name,
                    source=MetricSource.OBSERVABILITY,
                    resource_name=resource.name,
                )
            )

    return metrics


# PromQL functions and keywords to exclude from metric name extraction
_PROMQL_KEYWORDS = frozenset({
    "sum", "rate", "irate", "increase", "histogram_quantile", "avg", "min", "max",
    "count", "stddev", "stdvar", "topk", "bottomk", "quantile", "count_values",
    "group", "by", "without", "on", "ignoring", "group_left", "group_right",
    "bool", "and", "or", "unless", "offset", "vector", "scalar", "abs", "absent",
    "ceil", "floor", "round", "clamp", "clamp_max", "clamp_min", "day_of_month",
    "day_of_week", "days_in_month", "delta", "deriv", "exp", "hour", "idelta",
    "label_join", "label_replace", "ln", "log2", "log10", "minute", "month",
    "predict_linear", "resets", "sort", "sort_desc", "sqrt", "time", "timestamp",
    "year", "avg_over_time", "min_over_time", "max_over_time", "sum_over_time",
    "count_over_time", "quantile_over_time", "stddev_over_time", "stdvar_over_time",
    "last_over_time", "present_over_time", "changes", "le",
})


def _extract_metrics_from_query(query: str) -> list[str]:
    """Extract metric names from a PromQL query."""
    pattern = r"([a-zA-Z_:][a-zA-Z0-9_:]*)\s*(?:\{|\[|$)"
    matches = re.findall(pattern, query)

    return [
        m for m in matches
        if m.lower() not in _PROMQL_KEYWORDS and not m.startswith("__")
    ]
