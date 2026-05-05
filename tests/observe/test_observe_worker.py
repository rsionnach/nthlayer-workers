"""Tests for observe worker modules — collect, drift, topology.

Tests verify the worker module protocol, manifest fetching from core API,
Prometheus collection, portfolio synthesis, and assessment submission to core.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch


from nthlayer_common.api_client import APIResult
from nthlayer_workers.observe.worker import (
    ObserveCollectModule,
    ObserveDriftModule,
    ObserveTopologyModule,
    _drift_targets,
    _extract_service_slos,
)


def _manifest_response(services: list[dict] | None = None) -> APIResult:
    """Build a mock API response for GET /manifests."""
    if services is None:
        services = [
            {
                "name": "fraud-detect",
                "team": "payments-ml",
                "tier": "critical",
                "type": "ai-gate",
                "namespace": "default",
                "source_format": "srm_v1",
                "slos": [
                    {
                        "name": "reversal_rate",
                        "target": 1.5,
                        "slo_type": "availability",
                        "window": "2m",
                        "indicator_query": '1 - (sum(rate(gen_ai_overrides_total[2m])) / clamp_min(sum(rate(gen_ai_decisions_total[2m])), 1))',
                    },
                    {
                        "name": "availability",
                        "target": 99.9,
                        "slo_type": "availability",
                        "window": "30d",
                        "indicator_query": 'sum(rate(http_requests_total{service="fraud-detect",status="200"}[5m])) / sum(rate(http_requests_total{service="fraud-detect"}[5m]))',
                    },
                ],
                "dependencies": [],
            }
        ]
    return APIResult(ok=True, status_code=200, data=services)


# ---------------------------------------------------------------------------
# ObserveCollectModule protocol + happy path
# ---------------------------------------------------------------------------


class TestCollectModuleProtocol:
    def test_name(self):
        module = ObserveCollectModule(client=AsyncMock(), prometheus_url="http://prom:9090")
        assert module.name == "observe.collect"

    async def test_restore_state_accepts_none(self):
        module = ObserveCollectModule(client=AsyncMock(), prometheus_url="http://prom:9090")
        await module.restore_state(None)

    async def test_get_state_returns_empty_dict(self):
        module = ObserveCollectModule(client=AsyncMock(), prometheus_url="http://prom:9090")
        state = await module.get_state()
        assert state == {}

    def test_satisfies_worker_module_protocol(self):
        from nthlayer_workers.runner import WorkerModule

        module = ObserveCollectModule(client=AsyncMock(), prometheus_url="http://prom:9090")
        assert isinstance(module, WorkerModule)


class TestCollectCycleHappyPath:
    # ObserveCollectModule must be constructed INSIDE the @patch context —
    # __post_init__ creates the collector, so the class must be patched first.
    @patch("nthlayer_workers.observe.worker.SLOMetricCollector")
    async def test_fetches_manifests_collects_submits(self, mock_collector_cls):
        from nthlayer_workers.observe.slo.collector import SLOResult

        client = AsyncMock()
        client.get_manifests = AsyncMock(return_value=_manifest_response())
        client.submit_assessment = AsyncMock(return_value=APIResult(ok=True, status_code=201, data={"id": "asm-001"}))

        mock_collector = AsyncMock()
        mock_collector.collect = AsyncMock(return_value=[
            SLOResult("reversal_rate", 1.5, "2m", 0.03, current_sli=99.5, burned_minutes=0.01, percent_consumed=33.3, status="HEALTHY"),
            SLOResult("availability", 99.9, "30d", 43.2, current_sli=99.95, burned_minutes=10.0, percent_consumed=23.1, status="HEALTHY"),
        ])
        mock_collector_cls.return_value = mock_collector

        module = ObserveCollectModule(client=client, prometheus_url="http://prom:9090")
        await module.process_cycle()

        client.get_manifests.assert_awaited_once()
        mock_collector.collect.assert_awaited_once()
        # 2 slo_status + 1 portfolio_status = 3
        assert client.submit_assessment.await_count == 3

    @patch("nthlayer_workers.observe.worker.SLOMetricCollector")
    async def test_portfolio_assessment_submitted(self, mock_collector_cls):
        from nthlayer_workers.observe.slo.collector import SLOResult

        client = AsyncMock()
        client.get_manifests = AsyncMock(return_value=_manifest_response())
        submitted = []
        client.submit_assessment = AsyncMock(
            side_effect=lambda a: (submitted.append(a.get('data', a)), APIResult(ok=True, status_code=201, data={"id": "x"}))[1]
        )

        mock_collector = AsyncMock()
        mock_collector.collect = AsyncMock(return_value=[
            SLOResult("availability", 99.9, "30d", 43.2, current_sli=99.95, burned_minutes=10.0, percent_consumed=23.1, status="HEALTHY"),
        ])
        mock_collector_cls.return_value = mock_collector

        module = ObserveCollectModule(client=client, prometheus_url="http://prom:9090")
        await module.process_cycle()

        portfolio = [a for a in submitted if a.get("kind") == "portfolio_status"]
        assert len(portfolio) == 1
        assert portfolio[0]["service"] == "__portfolio__"
        assert portfolio[0]["data"]["total_services"] == 1
        assert portfolio[0]["data"]["healthy_count"] == 1

    @patch("nthlayer_workers.observe.worker.SLOMetricCollector")
    async def test_portfolio_parent_ids(self, mock_collector_cls):
        """Portfolio assessment references SLO assessment IDs as parent_ids."""
        from nthlayer_workers.observe.slo.collector import SLOResult

        client = AsyncMock()
        client.get_manifests = AsyncMock(return_value=_manifest_response())
        submitted = []
        client.submit_assessment = AsyncMock(
            side_effect=lambda a: (submitted.append(a.get('data', a)), APIResult(ok=True, status_code=201, data={"id": "x"}))[1]
        )

        mock_collector = AsyncMock()
        mock_collector.collect = AsyncMock(return_value=[
            SLOResult("avail", 99.9, "30d", 43.2, status="HEALTHY", current_sli=99.95, burned_minutes=10.0, percent_consumed=23.1),
        ])
        mock_collector_cls.return_value = mock_collector

        module = ObserveCollectModule(client=client, prometheus_url="http://prom:9090")
        await module.process_cycle()

        slo_ids = [a["id"] for a in submitted if a.get("kind") == "slo_status"]
        portfolio = [a for a in submitted if a.get("kind") == "portfolio_status"][0]
        assert portfolio["data"]["parent_ids"] == slo_ids
        assert len(slo_ids) > 0

    @patch("nthlayer_workers.observe.worker.SLOMetricCollector")
    async def test_slo_assessment_has_correct_kind(self, mock_collector_cls):
        from nthlayer_workers.observe.slo.collector import SLOResult

        client = AsyncMock()
        client.get_manifests = AsyncMock(return_value=_manifest_response())
        submitted = []
        client.submit_assessment = AsyncMock(
            side_effect=lambda a: (submitted.append(a.get('data', a)), APIResult(ok=True, status_code=201, data={"id": "x"}))[1]
        )

        mock_collector = AsyncMock()
        mock_collector.collect = AsyncMock(return_value=[
            SLOResult("availability", 99.9, "30d", 43.2, current_sli=99.95, burned_minutes=10.0, percent_consumed=23.1, status="HEALTHY"),
        ])
        mock_collector_cls.return_value = mock_collector

        module = ObserveCollectModule(client=client, prometheus_url="http://prom:9090")
        await module.process_cycle()

        slo_assessments = [a for a in submitted if a.get("kind") == "slo_status"]
        assert len(slo_assessments) == 1
        assert slo_assessments[0]["service"] == "fraud-detect"


class TestCollectCycleFailureModes:
    async def test_manifest_fetch_fails_no_crash(self):
        client = AsyncMock()
        client.get_manifests = AsyncMock(
            return_value=APIResult(ok=False, status_code=503, error="service_unavailable")
        )
        client.submit_assessment = AsyncMock()

        module = ObserveCollectModule(client=client, prometheus_url="http://prom:9090")
        await module.process_cycle()

        client.submit_assessment.assert_not_awaited()

    async def test_empty_manifest_list_no_crash(self):
        client = AsyncMock()
        client.get_manifests = AsyncMock(return_value=_manifest_response([]))
        client.submit_assessment = AsyncMock()

        module = ObserveCollectModule(client=client, prometheus_url="http://prom:9090")
        await module.process_cycle()

        client.submit_assessment.assert_not_awaited()

    @patch("nthlayer_workers.observe.worker.SLOMetricCollector")
    async def test_submit_fails_no_crash(self, mock_collector_cls):
        from nthlayer_workers.observe.slo.collector import SLOResult

        client = AsyncMock()
        client.get_manifests = AsyncMock(return_value=_manifest_response())
        client.submit_assessment = AsyncMock(
            return_value=APIResult(ok=False, status_code=503, error="service_unavailable")
        )

        mock_collector = AsyncMock()
        mock_collector.collect = AsyncMock(return_value=[
            SLOResult("availability", 99.9, "30d", 43.2, status="HEALTHY", current_sli=99.95, burned_minutes=10.0, percent_consumed=23.1),
        ])
        mock_collector_cls.return_value = mock_collector

        module = ObserveCollectModule(client=client, prometheus_url="http://prom:9090")
        await module.process_cycle()  # should not raise

    async def test_manifests_with_no_slos_skipped(self):
        client = AsyncMock()
        client.get_manifests = AsyncMock(return_value=_manifest_response([
            {
                "name": "bare-service",
                "team": "test",
                "tier": "standard",
                "type": "api",
                "namespace": "default",
                "source_format": "srm_v1",
                "slos": [],
                "dependencies": [],
            }
        ]))
        client.submit_assessment = AsyncMock()

        module = ObserveCollectModule(client=client, prometheus_url="http://prom:9090")
        await module.process_cycle()

        client.submit_assessment.assert_not_awaited()


# ---------------------------------------------------------------------------
# ObserveDriftModule
# ---------------------------------------------------------------------------


class TestDriftModuleProtocol:
    def test_name(self):
        module = ObserveDriftModule(client=AsyncMock(), prometheus_url="http://prom:9090")
        assert module.name == "observe.drift"

    async def test_get_state_returns_empty_dict(self):
        module = ObserveDriftModule(client=AsyncMock(), prometheus_url="http://prom:9090")
        assert await module.get_state() == {}

    def test_satisfies_worker_module_protocol(self):
        from nthlayer_workers.runner import WorkerModule

        module = ObserveDriftModule(client=AsyncMock(), prometheus_url="http://prom:9090")
        assert isinstance(module, WorkerModule)


class TestDriftTargets:
    def test_extracts_targets(self):
        manifests = [
            {"name": "svc-a", "tier": "critical", "slos": [
                {"name": "avail", "window": "30d"},
                {"name": "latency", "window": "7d"},
            ]},
            {"name": "svc-b", "tier": "standard", "slos": [
                {"name": "avail"},
            ]},
        ]
        targets = _drift_targets(manifests)
        assert len(targets) == 3
        assert targets[0] == ("svc-a", "critical", "avail", "30d")
        assert targets[1] == ("svc-a", "critical", "latency", "7d")
        assert targets[2] == ("svc-b", "standard", "avail", None)

    def test_skips_missing_name(self):
        manifests = [{"tier": "standard", "slos": [{"name": "avail"}]}]
        assert _drift_targets(manifests) == []

    def test_skips_slos_without_name(self):
        manifests = [{"name": "svc", "tier": "standard", "slos": [{"target": 99.9}]}]
        assert _drift_targets(manifests) == []


class TestDriftCycle:
    async def test_manifest_fetch_fails_no_crash(self):
        client = AsyncMock()
        client.get_manifests = AsyncMock(
            return_value=APIResult(ok=False, status_code=503, error="unavailable")
        )
        client.submit_assessment = AsyncMock()

        module = ObserveDriftModule(client=client, prometheus_url="http://prom:9090")
        await module.process_cycle()

        client.submit_assessment.assert_not_awaited()


# ---------------------------------------------------------------------------
# ObserveTopologyModule
# ---------------------------------------------------------------------------


class TestTopologyModuleProtocol:
    def test_name(self):
        module = ObserveTopologyModule(client=AsyncMock(), prometheus_url="http://prom:9090")
        assert module.name == "observe.topology"

    async def test_get_state_returns_empty_dict(self):
        module = ObserveTopologyModule(client=AsyncMock(), prometheus_url="http://prom:9090")
        assert await module.get_state() == {}

    def test_satisfies_worker_module_protocol(self):
        from nthlayer_workers.runner import WorkerModule

        module = ObserveTopologyModule(client=AsyncMock(), prometheus_url="http://prom:9090")
        assert isinstance(module, WorkerModule)


class TestTopologyCycle:
    async def test_manifest_fetch_fails_no_crash(self):
        client = AsyncMock()
        client.get_manifests = AsyncMock(
            return_value=APIResult(ok=False, status_code=503, error="unavailable")
        )
        client.submit_assessment = AsyncMock()

        module = ObserveTopologyModule(client=client, prometheus_url="http://prom:9090")
        await module.process_cycle()

        client.submit_assessment.assert_not_awaited()

    async def test_empty_manifests_no_crash(self):
        client = AsyncMock()
        client.get_manifests = AsyncMock(return_value=_manifest_response([]))
        client.submit_assessment = AsyncMock()

        module = ObserveTopologyModule(client=client, prometheus_url="http://prom:9090")
        await module.process_cycle()

        client.submit_assessment.assert_not_awaited()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class TestExtractServiceSLOs:
    def test_extracts_from_manifest(self):
        manifests = [
            {"name": "svc", "slos": [
                {"name": "avail", "target": 99.9, "slo_type": "availability", "window": "30d"},
            ]},
        ]
        results = _extract_service_slos(manifests)
        assert len(results) == 1
        assert results[0].service == "svc"
        assert results[0].slo.name == "avail"

    def test_skips_missing_name(self):
        manifests = [{"slos": [{"name": "avail", "target": 99.9}]}]
        assert _extract_service_slos(manifests) == []

    def test_skips_slo_missing_target(self):
        manifests = [{"name": "svc", "slos": [{"name": "avail"}]}]
        assert _extract_service_slos(manifests) == []
