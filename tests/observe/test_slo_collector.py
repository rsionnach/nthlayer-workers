"""Tests for nthlayer_observe.slo.collector module."""

from unittest.mock import AsyncMock, patch

import pytest
from nthlayer_common.manifest.models import SLODefinition

from nthlayer_workers.observe.slo.collector import (
    ServiceSLO,
    SLOMetricCollector,
    SLOResult,
    _determine_status,
    _parse_window_minutes,
    results_to_assessments,
)


@pytest.fixture
def availability_slo():
    return ServiceSLO(
        service="payment-api",
        slo=SLODefinition(
            name="availability",
            target=99.9,
            slo_type="availability",
            window="30d",
            indicator_query='up{job="payment"}',
        ),
    )


class TestSLOMetricCollector:
    def test_no_prometheus_url_raises(self):
        collector = SLOMetricCollector(prometheus_url=None)
        with pytest.raises(ValueError, match="Prometheus URL is required"):
            import asyncio

            asyncio.run(collector.collect([]))

    # What a collected SLO reads as is in test_slo_budget.py.

    @patch("nthlayer_workers.observe.slo.collector.PrometheusProvider")
    async def test_provider_closed_after_collect(self, mock_provider_cls, availability_slo):
        mock_provider = AsyncMock()
        mock_provider.query = AsyncMock(return_value={"data": {"result": []}})
        mock_provider.aclose = AsyncMock()
        mock_provider_cls.return_value = mock_provider

        collector = SLOMetricCollector("http://prom:9090")
        await collector.collect([availability_slo])

        mock_provider.aclose.assert_awaited_once()


class TestCalculateAggregateBudget:
    def test_aggregate_with_valid_results(self):
        results = [
            SLOResult("avail", 99.9, "30d", 43.2, burned_minutes=10.0),
            SLOResult("latency", 99.0, "30d", 432.0, burned_minutes=100.0),
        ]
        collector = SLOMetricCollector()
        budget = collector.calculate_aggregate_budget(results)
        assert budget.valid_slo_count == 2
        assert budget.total_budget_minutes == pytest.approx(475.2)
        assert budget.burned_budget_minutes == pytest.approx(110.0)

    def test_aggregate_no_valid_results(self):
        results = [SLOResult("avail", 99.9, "30d", 43.2)]  # no burned_minutes
        collector = SLOMetricCollector()
        budget = collector.calculate_aggregate_budget(results)
        assert budget.valid_slo_count == 0
        assert budget.remaining_percent == 100

    def test_aggregate_empty(self):
        collector = SLOMetricCollector()
        budget = collector.calculate_aggregate_budget([])
        assert budget.valid_slo_count == 0


class TestHelpers:
    def test_parse_window_minutes_days(self):
        assert _parse_window_minutes("30d") == 43200

    def test_parse_window_minutes_hours(self):
        assert _parse_window_minutes("24h") == 1440

    def test_parse_window_minutes_weeks(self):
        assert _parse_window_minutes("1w") == 10080

    def test_parse_window_minutes_minutes(self):
        assert _parse_window_minutes("2m") == 2

    def test_parse_window_minutes_unknown(self):
        assert _parse_window_minutes("unknown") == 43200  # default 30d

    def test_determine_status_healthy(self):
        assert _determine_status(30) == "HEALTHY"

    def test_determine_status_warning(self):
        assert _determine_status(50) == "WARNING"

    def test_determine_status_critical(self):
        assert _determine_status(80) == "CRITICAL"

    def test_determine_status_exhausted(self):
        assert _determine_status(100) == "EXHAUSTED"

    def test_determine_status_boundary(self):
        assert _determine_status(49.9) == "HEALTHY"
        assert _determine_status(79.9) == "WARNING"
        assert _determine_status(99.9) == "CRITICAL"


class TestResultsToAssessments:
    def test_converts_healthy_result(self):
        results = [
            SLOResult(
                "avail", 99.9, "30d", 43.2,
                current_sli=99.95, burned_minutes=10.0,
                percent_consumed=23.1, status="HEALTHY",
            )
        ]
        assessments = results_to_assessments(results, "payment-api")

        assert len(assessments) == 1
        a = assessments[0]
        assert a.kind == "slo_status"
        assert a.service == "payment-api"
        assert a.producer == "nthlayer-observe"
        assert a.data["slo_name"] == "avail"
        assert a.data["objective"] == 99.9
        assert a.data["status"] == "HEALTHY"
        assert a.data["current_sli"] == 99.95
        assert "error" not in a.data

    def test_converts_error_result(self):
        results = [
            SLOResult("avail", 99.9, "30d", 43.2, status="ERROR", error="timeout")
        ]
        assessments = results_to_assessments(results, "svc")

        assert assessments[0].data["status"] == "ERROR"
        assert assessments[0].data["error"] == "timeout"
        assert "current_sli" not in assessments[0].data

    def test_multiple_results(self):
        results = [
            SLOResult("a", 99.9, "30d", 43.2, status="HEALTHY", burned_minutes=5.0),
            SLOResult("b", 99.0, "7d", 10.0, status="WARNING", burned_minutes=6.0),
        ]
        assessments = results_to_assessments(results, "svc")
        assert len(assessments) == 2
        assert assessments[0].data["slo_name"] == "a"
        assert assessments[1].data["slo_name"] == "b"
