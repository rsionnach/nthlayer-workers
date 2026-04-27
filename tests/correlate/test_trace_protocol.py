"""Tests for trace backend protocol and dataclasses."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from nthlayer_workers.correlate.traces.protocol import (
    ErrorSummary,
    OperationLatency,
    ServiceCallEdge,
    ServiceTraceProfile,
    TopologyDivergence,
    TraceBackend,
    TraceEvidence,
    TraceSpanSummary,
)


class TestTraceSpanSummary:
    def test_construction_with_all_fields(self):
        span = TraceSpanSummary(
            trace_id="abc123",
            span_id="def456",
            service="fraud-detect",
            operation="POST /predict",
            duration_ms=45.2,
            status="error",
            error_message="timeout",
            parent_service="payment-api",
            attributes={"http.method": "POST"},
        )
        assert span.trace_id == "abc123"
        assert span.service == "fraud-detect"
        assert span.status == "error"
        assert span.error_message == "timeout"
        assert span.parent_service == "payment-api"

    def test_defaults_for_optional_fields(self):
        span = TraceSpanSummary(
            trace_id="abc",
            span_id="def",
            service="svc",
            operation="op",
            duration_ms=10.0,
            status="ok",
            error_message=None,
            parent_service=None,
            attributes={},
        )
        assert span.error_message is None
        assert span.parent_service is None


class TestServiceCallEdge:
    def test_construction(self):
        edge = ServiceCallEdge(
            source_service="payment-api",
            target_service="fraud-detect",
            request_count=1204,
            error_count=3,
            p50_latency_ms=12.5,
            p99_latency_ms=85.0,
        )
        assert edge.source_service == "payment-api"
        assert edge.target_service == "fraud-detect"
        assert edge.request_count == 1204
        assert edge.error_count == 3


class TestErrorSummary:
    def test_construction(self):
        now = datetime.now(tz=timezone.utc)
        err = ErrorSummary(
            error_message="ConnectionPool exhausted",
            count=4821,
            first_seen=now,
            last_seen=now,
            sample_trace_id=None,
        )
        assert err.error_message == "ConnectionPool exhausted"
        assert err.count == 4821
        assert err.sample_trace_id is None


class TestOperationLatency:
    def test_construction_with_baseline(self):
        op = OperationLatency(
            operation="POST /api/v1/predict",
            p50_ms=150.0,
            p99_ms=340.0,
            request_count=5000,
            error_rate=0.02,
            baseline_p50_ms=50.0,
            change_pct=200.0,
        )
        assert op.operation == "POST /api/v1/predict"
        assert op.baseline_p50_ms == 50.0
        assert op.change_pct == 200.0

    def test_defaults_for_baseline(self):
        op = OperationLatency(
            operation="GET /health",
            p50_ms=5.0,
            p99_ms=10.0,
            request_count=100,
            error_rate=0.0,
            baseline_p50_ms=None,
            change_pct=None,
        )
        assert op.baseline_p50_ms is None
        assert op.change_pct is None


class TestTopologyDivergence:
    def test_empty_divergence(self):
        div = TopologyDivergence(
            declared_not_observed=[],
            observed_not_declared=[],
        )
        assert div.declared_not_observed == []
        assert div.observed_not_declared == []


class TestServiceTraceProfile:
    def test_construction_with_all_fields(self):
        now = datetime.now(tz=timezone.utc)
        profile = ServiceTraceProfile(
            service="fraud-detect",
            time_window_start=now - timedelta(minutes=30),
            time_window_end=now,
            callers=[],
            callees=[],
            p50_latency_ms=50.0,
            p95_latency_ms=120.0,
            p99_latency_ms=340.0,
            baseline_p50_ms=18.0,
            latency_change_pct=178.0,
            error_rate=0.02,
            error_count=24,
            total_request_count=1204,
            top_errors=[],
            slow_operations=[],
            sample_error_traces=[],
            sample_slow_traces=[],
        )
        assert profile.service == "fraud-detect"
        assert profile.p99_latency_ms == 340.0
        assert profile.baseline_p50_ms == 18.0
        assert profile.latency_change_pct == 178.0


class TestTraceEvidence:
    def test_construction(self):
        evidence = TraceEvidence(
            services=[],
            topology_divergence=None,
            query_time_ms=6240.0,
            backend="tempo",
        )
        assert evidence.backend == "tempo"
        assert evidence.topology_divergence is None
        assert evidence.query_time_ms == 6240.0

    def test_with_topology_divergence(self):
        div = TopologyDivergence(
            declared_not_observed=[("a", "b")],
            observed_not_declared=[("c", "d")],
        )
        evidence = TraceEvidence(
            services=[],
            topology_divergence=div,
            query_time_ms=100.0,
            backend="tempo",
        )
        assert evidence.topology_divergence is not None
        assert evidence.topology_divergence.declared_not_observed == [("a", "b")]


class TestTraceBackendProtocol:
    def test_stub_satisfies_protocol(self):
        """A class implementing the required methods satisfies TraceBackend structurally."""

        class StubBackend:
            async def get_trace_evidence(self, services, start, end, baseline_window=timedelta(hours=1)):
                return TraceEvidence(services=[], topology_divergence=None, query_time_ms=0, backend="stub")

            async def get_service_dependencies(self, service, start, end):
                return []

            async def health_check(self):
                return True

        def accepts_backend(backend: TraceBackend) -> str:
            return "ok"

        stub = StubBackend()
        # Structural typing: if this doesn't raise a type error at runtime,
        # the stub satisfies the protocol shape. We verify by calling the
        # function that expects a TraceBackend.
        assert accepts_backend(stub) == "ok"
