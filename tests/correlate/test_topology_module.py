"""Tests for CorrelateTopologyModule — topology drift detection."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import AsyncMock


from nthlayer_common.api_client import APIResult
from nthlayer_workers.correlate.worker import (
    CorrelateTopologyModule,
    _check_guarantee_mismatches,
    _parse_latency_ms,
)


def _manifest_response(manifests=None):
    if manifests is None:
        manifests = [
            {
                "name": "payment-api",
                "tier": "critical",
                "type": "api",
                "slos": [],
                "dependencies": [
                    {"name": "fraud-detect", "type": "ai-gate", "critical": True,
                     "slo": {"availability": 0.999, "latency_p99": "200ms"}},
                ],
                "contracts": [],
            },
            {
                "name": "fraud-detect",
                "tier": "critical",
                "type": "ai-gate",
                "slos": [],
                "dependencies": [],
                "contracts": [],
            },
        ]
    return APIResult(ok=True, status_code=200, data=manifests)


@dataclass
class MockServiceCallEdge:
    source_service: str
    target_service: str
    request_count: int = 1000
    error_count: int = 0
    p50_latency_ms: float = 50.0
    p99_latency_ms: float = 150.0


@dataclass
class MockServiceProfile:
    service: str
    callees: list = field(default_factory=list)
    callers: list = field(default_factory=list)


@dataclass
class MockTraceEvidence:
    services: list = field(default_factory=list)
    topology_divergence: object = None
    query_time_ms: float = 100.0
    backend: str = "tempo"


class TestTopologyModuleProtocol:
    def test_name(self):
        module = CorrelateTopologyModule(client=AsyncMock())
        assert module.name == "correlate.topology"

    async def test_get_state_empty(self):
        module = CorrelateTopologyModule(client=AsyncMock())
        assert await module.get_state() == {}

    def test_satisfies_protocol(self):
        from nthlayer_workers.runner import WorkerModule
        module = CorrelateTopologyModule(client=AsyncMock())
        assert isinstance(module, WorkerModule)

    async def test_restore_state_accepts_none(self):
        module = CorrelateTopologyModule(client=AsyncMock())
        await module.restore_state(None)

    async def test_noop_when_not_configured(self):
        client = AsyncMock()
        client.submit_assessment = AsyncMock()
        module = CorrelateTopologyModule(client=client, trace_backend=None)
        await module.process_cycle()
        client.submit_assessment.assert_not_awaited()


class TestTopologyCycle:
    async def test_manifest_fetch_fails_no_crash(self):
        backend = AsyncMock()
        client = AsyncMock()
        client.get_manifests = AsyncMock(
            return_value=APIResult(ok=False, status_code=503, error="unavailable")
        )
        module = CorrelateTopologyModule(client=client, trace_backend=backend)
        await module.process_cycle()

    async def test_no_drift_no_assessment(self):
        """Declared == observed → no assessment emitted."""
        backend = AsyncMock()
        backend.get_trace_evidence = AsyncMock(return_value=MockTraceEvidence(
            services=[
                MockServiceProfile(
                    service="payment-api",
                    callees=[MockServiceCallEdge("payment-api", "fraud-detect")],
                ),
                MockServiceProfile(service="fraud-detect"),
            ]
        ))

        client = AsyncMock()
        client.get_manifests = AsyncMock(return_value=_manifest_response())
        client.submit_assessment = AsyncMock()

        module = CorrelateTopologyModule(client=client, trace_backend=backend)
        await module.process_cycle()

        client.submit_assessment.assert_not_awaited()

    async def test_drift_produces_assessment(self):
        """Observed edge not in declared → topology_drift assessment emitted."""
        backend = AsyncMock()
        backend.get_trace_evidence = AsyncMock(return_value=MockTraceEvidence(
            services=[
                MockServiceProfile(
                    service="payment-api",
                    callees=[
                        MockServiceCallEdge("payment-api", "fraud-detect"),
                        MockServiceCallEdge("payment-api", "analytics-api"),  # undeclared
                    ],
                ),
                MockServiceProfile(service="fraud-detect"),
            ]
        ))

        client = AsyncMock()
        client.get_manifests = AsyncMock(return_value=_manifest_response())
        submitted = []
        client.submit_assessment = AsyncMock(
            side_effect=lambda a: (submitted.append(a), APIResult(ok=True, status_code=201, data={"id": "x"}))[1]
        )

        module = CorrelateTopologyModule(client=client, trace_backend=backend)
        await module.process_cycle()

        assert len(submitted) == 1
        assert submitted[0]["kind"] == "topology_drift"

    async def test_tempo_unreachable_no_crash(self):
        backend = AsyncMock()
        backend.get_trace_evidence = AsyncMock(side_effect=Exception("connection refused"))

        client = AsyncMock()
        client.get_manifests = AsyncMock(return_value=_manifest_response())
        client.submit_assessment = AsyncMock()

        module = CorrelateTopologyModule(client=client, trace_backend=backend)
        await module.process_cycle()

        client.submit_assessment.assert_not_awaited()


class TestGuaranteeMismatches:
    def test_availability_breach(self):
        manifests = [{
            "name": "svc-a",
            "dependencies": [
                {"name": "svc-b", "slo": {"availability": 0.999}},
            ],
        }]
        evidence = MockTraceEvidence(services=[
            MockServiceProfile(
                service="svc-a",
                callees=[MockServiceCallEdge("svc-a", "svc-b", request_count=1000, error_count=50)],
            ),
        ])
        mismatches = _check_guarantee_mismatches(manifests, evidence)
        assert len(mismatches) == 1
        assert mismatches[0]["metric"] == "availability"
        assert mismatches[0]["promised"] == 0.999
        assert mismatches[0]["observed"] < 0.999

    def test_no_breach_no_mismatch(self):
        manifests = [{
            "name": "svc-a",
            "dependencies": [
                {"name": "svc-b", "slo": {"availability": 0.99}},
            ],
        }]
        evidence = MockTraceEvidence(services=[
            MockServiceProfile(
                service="svc-a",
                callees=[MockServiceCallEdge("svc-a", "svc-b", request_count=1000, error_count=1)],
            ),
        ])
        assert _check_guarantee_mismatches(manifests, evidence) == []


class TestParseLatency:
    def test_milliseconds(self):
        assert _parse_latency_ms("200ms") == 200.0

    def test_seconds(self):
        assert _parse_latency_ms("1s") == 1000.0

    def test_invalid(self):
        assert _parse_latency_ms("fast") is None
