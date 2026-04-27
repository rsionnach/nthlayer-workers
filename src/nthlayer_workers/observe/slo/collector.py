"""Stateless SLO metric collector — queries Prometheus and produces Assessments."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import structlog

from nthlayer_common.manifest.models import SLODefinition
from nthlayer_common.providers import PrometheusProvider
from nthlayer_common.providers.prometheus import PrometheusProviderError

from nthlayer_workers.observe.assessment import Assessment, create

logger = structlog.get_logger()

STATUS_THRESHOLDS = {
    100: "EXHAUSTED",
    80: "CRITICAL",
    50: "WARNING",
}


@dataclass
class ServiceSLO:
    """SLO with its owning service name.

    Bridges manifest-level context to the collector. The common
    SLODefinition doesn't carry a service field — it's a property
    of ReliabilityManifest. This carrier adds the one field the
    collector needs.
    """

    service: str
    slo: SLODefinition


@dataclass
class SLOResult:
    """Result of collecting metrics for a single SLO."""

    name: str
    objective: float
    window: str
    total_budget_minutes: float
    current_sli: float | None = None
    burned_minutes: float | None = None
    percent_consumed: float | None = None
    status: str = "UNKNOWN"
    error: str | None = None


@dataclass
class BudgetSummary:
    """Aggregate budget summary across all SLOs."""

    total_budget_minutes: float
    burned_budget_minutes: float
    remaining_percent: float
    consumed_percent: float
    valid_slo_count: int


class SLOMetricCollector:
    """Stateless SLO metric collector."""

    def __init__(self, prometheus_url: str | None = None) -> None:
        self.prometheus_url = prometheus_url
        self._username = os.environ.get("PROMETHEUS_USERNAME") or os.environ.get(
            "NTHLAYER_METRICS_USER"
        )
        self._password = os.environ.get("PROMETHEUS_PASSWORD") or os.environ.get(
            "NTHLAYER_METRICS_PASSWORD"
        )

    async def collect(self, slo_definitions: list[ServiceSLO]) -> list[SLOResult]:
        """Collect SLO metrics from Prometheus."""
        if not self.prometheus_url:
            raise ValueError("Prometheus URL is required for metric collection")

        provider = PrometheusProvider(
            self.prometheus_url, username=self._username, password=self._password
        )
        results = []
        try:
            for item in slo_definitions:
                result = await self._collect_single_slo(item, provider)
                results.append(result)
        finally:
            await provider.aclose()

        return results

    async def _collect_single_slo(
        self, item: ServiceSLO, provider: PrometheusProvider
    ) -> SLOResult:
        """Collect metrics for a single SLO."""
        slo = item.slo
        objective = slo.target
        window = slo.window

        window_minutes = _parse_window_minutes(window)
        error_budget_percent = (100 - objective) / 100
        total_budget_minutes = window_minutes * error_budget_percent

        result = SLOResult(
            name=slo.name,
            objective=objective,
            window=window,
            total_budget_minutes=total_budget_minutes,
        )

        query = _build_slo_query(slo, item.service)

        if query is None:
            result.error = "No query defined"
            result.status = "NO_DATA"
            return result

        try:
            sli_value = await provider.get_sli_value(query)

            # PrometheusProvider.get_sli_value returns 0.0 for both
            # "empty result set" and "value is 0.0". We treat 0.0 as a
            # valid measurement (total outage = EXHAUSTED) rather than
            # misclassifying it as NO_DATA. True no-data requires the
            # provider to return None (future nthlayer-common change).
            result.current_sli = sli_value * 100
            error_rate = 1.0 - sli_value
            result.burned_minutes = window_minutes * error_rate
            result.percent_consumed = (
                (result.burned_minutes / total_budget_minutes) * 100
                if total_budget_minutes > 0
                else 0
            )
            result.status = _determine_status(result.percent_consumed)

        except PrometheusProviderError as e:
            logger.warning("prometheus_query_failed", slo=slo.name, error=str(e))
            result.error = str(e)
            result.status = "ERROR"
        except Exception as e:
            logger.warning("unexpected_query_error", slo=slo.name, error=str(e), exc_info=True)
            result.error = str(e)
            result.status = "ERROR"

        return result

    def calculate_aggregate_budget(self, results: list[SLOResult]) -> BudgetSummary:
        """Calculate aggregate budget across all SLOs."""
        valid_results = [r for r in results if r.burned_minutes is not None]

        if not valid_results:
            return BudgetSummary(
                total_budget_minutes=0,
                burned_budget_minutes=0,
                remaining_percent=100,
                consumed_percent=0,
                valid_slo_count=0,
            )

        total_budget = sum(r.total_budget_minutes for r in valid_results)
        burned_budget = sum(r.burned_minutes or 0 for r in valid_results)

        remaining_pct = (
            (total_budget - burned_budget) / total_budget * 100 if total_budget > 0 else 100
        )

        return BudgetSummary(
            total_budget_minutes=total_budget,
            burned_budget_minutes=burned_budget,
            remaining_percent=remaining_pct,
            consumed_percent=100 - remaining_pct,
            valid_slo_count=len(valid_results),
        )


def results_to_assessments(results: list[SLOResult], service: str) -> list[Assessment]:
    """Convert SLOResults to slo_status Assessments."""
    assessments = []
    for r in results:
        data: dict[str, Any] = {
            "slo_name": r.name,
            "objective": r.objective,
            "window": r.window,
            "total_budget_minutes": r.total_budget_minutes,
            "status": r.status,
        }
        if r.current_sli is not None:
            data["current_sli"] = r.current_sli
        if r.burned_minutes is not None:
            data["burned_minutes"] = r.burned_minutes
        if r.percent_consumed is not None:
            data["percent_consumed"] = r.percent_consumed
        if r.error is not None:
            data["error"] = r.error

        assessments.append(create("slo_status", service, data))
    return assessments


def _build_slo_query(slo: SLODefinition, service_name: str) -> str | None:
    """Build PromQL query from SLO definition."""
    query = slo.indicator_query

    if not query and slo.total_query and slo.good_query:
        query = f"({slo.good_query}) / ({slo.total_query})"

    if query:
        query = query.replace("${service}", service_name)
        query = query.replace("$service", service_name)

    return query


def _determine_status(percent_consumed: float) -> str:
    """Determine SLO status based on budget consumption."""
    for threshold, status in sorted(STATUS_THRESHOLDS.items(), reverse=True):
        if percent_consumed >= threshold:
            return status
    return "HEALTHY"


def _parse_window_minutes(window: str) -> float:
    """Parse window string like '30d' into minutes."""
    SUFFIXES = {"d": 24 * 60, "h": 60, "w": 7 * 24 * 60, "m": 1}
    if window and window[-1] in SUFFIXES:
        try:
            return int(window[:-1]) * SUFFIXES[window[-1]]
        except ValueError:
            pass
    logger.warning("unknown_window_format", window=window, default="30d")
    return 30 * 24 * 60  # Default 30 days
