"""Tests for the portfolio aggregation module."""

from __future__ import annotations

import pytest

from nthlayer_workers.observe.assessment import create
from nthlayer_workers.observe.portfolio.aggregator import (
    SLOHealth,
    ServiceHealth,
    build_portfolio,
    build_portfolio_from_results,
)
from nthlayer_workers.observe.slo.collector import SLOResult
from nthlayer_workers.observe.portfolio.scorer import score_service
from nthlayer_workers.observe.store import MemoryAssessmentStore


def _make_slo_assessment(service: str, slo_name: str, status: str, **kwargs):
    """Helper to create an slo_status assessment."""
    data = {"slo_name": slo_name, "status": status, "objective": 99.9, "window": "30d"}
    data.update(kwargs)
    return create("slo_status", service, data)


class TestBuildPortfolio:
    def test_empty_store(self):
        store = MemoryAssessmentStore()
        summary = build_portfolio(store)
        assert summary.total_services == 0
        assert summary.services == []

    def test_single_service_healthy(self):
        store = MemoryAssessmentStore()
        store.put(_make_slo_assessment("payment-api", "availability", "HEALTHY", current_sli=99.95))
        store.put(_make_slo_assessment("payment-api", "latency", "HEALTHY", current_sli=99.8))

        summary = build_portfolio(store)
        assert summary.total_services == 1
        assert summary.healthy_count == 1
        assert summary.services[0].service == "payment-api"
        assert summary.services[0].overall_status == "HEALTHY"
        assert len(summary.services[0].slos) == 2

    def test_multiple_services(self):
        store = MemoryAssessmentStore()
        store.put(_make_slo_assessment("svc-a", "avail", "HEALTHY"))
        store.put(_make_slo_assessment("svc-b", "avail", "WARNING", percent_consumed=55.0))
        store.put(_make_slo_assessment("svc-c", "avail", "CRITICAL", percent_consumed=85.0))

        summary = build_portfolio(store)
        assert summary.total_services == 3
        assert summary.healthy_count == 1
        assert summary.warning_count == 1
        assert summary.critical_count == 1

    def test_overall_status_is_worst_slo(self):
        store = MemoryAssessmentStore()
        store.put(_make_slo_assessment("svc", "avail", "HEALTHY"))
        store.put(_make_slo_assessment("svc", "latency", "CRITICAL"))

        summary = build_portfolio(store)
        assert summary.services[0].overall_status == "CRITICAL"

    def test_exhausted_service(self):
        store = MemoryAssessmentStore()
        store.put(_make_slo_assessment("svc", "avail", "EXHAUSTED", percent_consumed=120.0))

        summary = build_portfolio(store)
        assert summary.exhausted_count == 1

    def test_deduplicates_by_latest(self):
        store = MemoryAssessmentStore()
        # Two assessments for same service+slo — second has later timestamp
        store.put(_make_slo_assessment("svc", "avail", "CRITICAL"))
        store.put(_make_slo_assessment("svc", "avail", "HEALTHY"))

        summary = build_portfolio(store)
        # Only one SLO entry (deduplicated)
        assert len(summary.services[0].slos) == 1
        # Second assessment is newer (later timestamp), query returns desc, so it's first
        assert summary.services[0].slos[0].status == "HEALTHY"

    def test_services_sorted_alphabetically(self):
        store = MemoryAssessmentStore()
        store.put(_make_slo_assessment("zebra", "avail", "HEALTHY"))
        store.put(_make_slo_assessment("alpha", "avail", "HEALTHY"))

        summary = build_portfolio(store)
        assert summary.services[0].service == "alpha"
        assert summary.services[1].service == "zebra"


class TestBuildPortfolioFromResults:
    def test_empty_results(self):
        summary = build_portfolio_from_results({})
        assert summary.total_services == 0
        assert summary.services == []

    def test_single_service_healthy(self):
        results = {
            "payment-api": [
                SLOResult("availability", 99.9, "30d", 43.2, current_sli=99.95, status="HEALTHY", percent_consumed=23.1),
                SLOResult("latency", 99.0, "30d", 432.0, current_sli=99.8, status="HEALTHY", percent_consumed=10.0),
            ],
        }
        summary = build_portfolio_from_results(results)
        assert summary.total_services == 1
        assert summary.healthy_count == 1
        assert summary.services[0].service == "payment-api"
        assert len(summary.services[0].slos) == 2

    def test_multiple_services_mixed_status(self):
        results = {
            "svc-a": [SLOResult("avail", 99.9, "30d", 43.2, status="HEALTHY")],
            "svc-b": [SLOResult("avail", 99.9, "30d", 43.2, status="WARNING", percent_consumed=55.0)],
            "svc-c": [SLOResult("avail", 99.9, "30d", 43.2, status="EXHAUSTED", percent_consumed=120.0)],
        }
        summary = build_portfolio_from_results(results)
        assert summary.total_services == 3
        assert summary.healthy_count == 1
        assert summary.warning_count == 1
        assert summary.exhausted_count == 1

    def test_worst_status_across_slos(self):
        results = {
            "svc": [
                SLOResult("avail", 99.9, "30d", 43.2, status="HEALTHY"),
                SLOResult("latency", 99.0, "30d", 432.0, status="CRITICAL"),
            ],
        }
        summary = build_portfolio_from_results(results)
        assert summary.services[0].overall_status == "CRITICAL"

    def test_services_sorted(self):
        results = {
            "zebra": [SLOResult("avail", 99.9, "30d", 43.2, status="HEALTHY")],
            "alpha": [SLOResult("avail", 99.9, "30d", 43.2, status="HEALTHY")],
        }
        summary = build_portfolio_from_results(results)
        assert summary.services[0].service == "alpha"
        assert summary.services[1].service == "zebra"


class TestServiceHealth:
    def test_post_init_computes_status(self):
        svc = ServiceHealth(
            service="svc",
            slos=[SLOHealth("a", "HEALTHY"), SLOHealth("b", "WARNING")],
        )
        assert svc.overall_status == "WARNING"

    def test_empty_slos_stays_unknown(self):
        svc = ServiceHealth(service="svc")
        assert svc.overall_status == "UNKNOWN"


class TestScorer:
    def test_all_healthy(self):
        svc = ServiceHealth(
            service="svc",
            slos=[SLOHealth("a", "HEALTHY"), SLOHealth("b", "HEALTHY")],
        )
        assert score_service(svc) == 100.0

    def test_mixed_status(self):
        svc = ServiceHealth(
            service="svc",
            slos=[SLOHealth("a", "HEALTHY"), SLOHealth("b", "CRITICAL")],
        )
        assert score_service(svc) == 50.0

    def test_no_slos(self):
        svc = ServiceHealth(service="svc")
        assert score_service(svc) == 0.0

    def test_all_critical(self):
        svc = ServiceHealth(
            service="svc",
            slos=[SLOHealth("a", "CRITICAL"), SLOHealth("b", "EXHAUSTED")],
        )
        assert score_service(svc) == 0.0


class TestPortfolioCLI:
    def test_portfolio_help(self, capsys):
        from nthlayer_workers.observe.cli import main

        with pytest.raises(SystemExit) as exc_info:
            main(["portfolio", "--help"])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "--store" in captured.out
        assert "--format" in captured.out

    def test_scorecard_help(self, capsys):
        from nthlayer_workers.observe.cli import main

        with pytest.raises(SystemExit) as exc_info:
            main(["scorecard", "--help"])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "--store" in captured.out
