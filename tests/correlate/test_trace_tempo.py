"""Tests for Grafana Tempo trace backend adapter."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from nthlayer_workers.correlate.traces.tempo import TempoTraceBackend

# ---------------------------------------------------------------------------
# Task 2: Constructor + health check + low-level HTTP transport
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_defaults(self):
        backend = TempoTraceBackend()
        assert backend.endpoint == "http://localhost:3200"
        assert backend.org_id == ""
        assert backend.use_service_graphs is True
        assert backend.prometheus_url is None

    def test_explicit_params(self):
        backend = TempoTraceBackend(
            endpoint="http://tempo:3200",
            org_id="tenant-1",
            timeout=60,
            use_service_graphs=False,
            prometheus_url="http://prom:9090",
        )
        assert backend.endpoint == "http://tempo:3200"
        assert backend.org_id == "tenant-1"
        assert backend.use_service_graphs is False
        assert backend.prometheus_url == "http://prom:9090"

    def test_org_id_sets_header(self):
        backend = TempoTraceBackend(org_id="my-tenant")
        assert backend._client.headers["X-Scope-OrgID"] == "my-tenant"

    def test_no_org_id_no_header(self):
        backend = TempoTraceBackend()
        assert "X-Scope-OrgID" not in backend._client.headers

    def test_endpoint_from_env(self, monkeypatch):
        monkeypatch.setenv("TEMPO_ENDPOINT", "http://env-tempo:3200")
        backend = TempoTraceBackend()
        assert backend.endpoint == "http://env-tempo:3200"


class TestHealthCheck:
    async def test_success(self):
        backend = TempoTraceBackend()
        mock_response = MagicMock()
        mock_response.status_code = 200
        backend._client.get = AsyncMock(return_value=mock_response)
        assert await backend.health_check() is True

    async def test_failure_status(self):
        backend = TempoTraceBackend()
        mock_response = MagicMock()
        mock_response.status_code = 503
        backend._client.get = AsyncMock(return_value=mock_response)
        assert await backend.health_check() is False

    async def test_connection_error(self):
        backend = TempoTraceBackend()
        backend._client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        assert await backend.health_check() is False


class TestTraceQLMetricsQuery:
    async def test_sends_correct_params(self):
        backend = TempoTraceBackend(endpoint="http://tempo:3200")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"series": []}
        mock_response.raise_for_status = MagicMock()
        backend._client.get = AsyncMock(return_value=mock_response)

        start = datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 10, 10, 30, 0, tzinfo=timezone.utc)

        result = await backend._traceql_metrics_query(
            '{ resource.service.name = "svc" }', start, end, "60s"
        )

        call_kwargs = backend._client.get.call_args
        assert call_kwargs[0][0] == "http://tempo:3200/api/metrics/query_range"
        params = call_kwargs[1]["params"]
        assert params["q"] == '{ resource.service.name = "svc" }'
        assert params["step"] == "60s"
        assert result == {"series": []}


class TestTraceQLSearch:
    async def test_sends_correct_params(self):
        backend = TempoTraceBackend(endpoint="http://tempo:3200")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"traces": [{"traceID": "abc"}]}
        mock_response.raise_for_status = MagicMock()
        backend._client.get = AsyncMock(return_value=mock_response)

        start = datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 10, 10, 30, 0, tzinfo=timezone.utc)

        result = await backend._traceql_search(
            '{ resource.service.name = "svc" }', start, end, limit=5
        )

        call_kwargs = backend._client.get.call_args
        assert call_kwargs[0][0] == "http://tempo:3200/api/search"
        params = call_kwargs[1]["params"]
        assert params["limit"] == "5"
        assert result == [{"traceID": "abc"}]


class TestPrometheusQuery:
    async def test_sends_correct_params(self):
        backend = TempoTraceBackend(prometheus_url="http://prom:9090")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": {"result": []}}
        mock_response.raise_for_status = MagicMock()
        backend._client.get = AsyncMock(return_value=mock_response)

        at = datetime(2026, 4, 10, 10, 30, 0, tzinfo=timezone.utc)
        result = await backend._prometheus_query("up", at)

        call_kwargs = backend._client.get.call_args
        assert call_kwargs[0][0] == "http://prom:9090/api/v1/query"
        assert result == {"data": {"result": []}}

    async def test_no_url_returns_empty(self):
        backend = TempoTraceBackend(prometheus_url=None)
        result = await backend._prometheus_query("up", datetime.now(tz=timezone.utc))
        assert result == {}


# ---------------------------------------------------------------------------
# Task 3: Result parsing helpers
# ---------------------------------------------------------------------------


class TestExtractMetricByService:
    def test_single_series(self):
        backend = TempoTraceBackend()
        response = {
            "series": [{
                "labels": {"resource.service.name": "fraud-detect"},
                "samples": [[1712700000, 50_000_000.0], [1712700060, 60_000_000.0]],
            }]
        }
        result = backend._extract_metric_by_service(response)
        assert result == {"fraud-detect": 55_000_000.0}

    def test_multiple_series(self):
        backend = TempoTraceBackend()
        response = {
            "series": [
                {
                    "labels": {"resource.service.name": "svc-a"},
                    "samples": [[1, 100.0]],
                },
                {
                    "labels": {"resource.service.name": "svc-b"},
                    "samples": [[1, 200.0]],
                },
            ]
        }
        result = backend._extract_metric_by_service(response)
        assert result == {"svc-a": 100.0, "svc-b": 200.0}

    def test_empty_response(self):
        backend = TempoTraceBackend()
        result = backend._extract_metric_by_service({"series": []})
        assert result == {}

    def test_promLabels_fallback(self):
        backend = TempoTraceBackend()
        response = {
            "series": [{
                "labels": {},
                "promLabels": {"resource_service_name": "svc-x"},
                "samples": [[1, 42.0]],
            }]
        }
        result = backend._extract_metric_by_service(response)
        assert result == {"svc-x": 42.0}


class TestExtractMetricByServiceAndOp:
    def test_faceted_response(self):
        backend = TempoTraceBackend()
        response = {
            "series": [{
                "labels": {
                    "resource.service.name": "fraud-detect",
                    "name": "POST /predict",
                },
                "samples": [[1, 100.0], [2, 200.0]],
            }]
        }
        result = backend._extract_metric_by_service_and_op(response)
        assert result == {("fraud-detect", "POST /predict"): 150.0}


class TestParseServiceGraphResults:
    def test_parses_prometheus_results(self):
        backend = TempoTraceBackend()
        count_data = {"data": {"result": [
            {"metric": {"client": "a", "server": "b"}, "value": [0, "100"]},
        ]}}
        error_data = {"data": {"result": [
            {"metric": {"client": "a", "server": "b"}, "value": [0, "5"]},
        ]}}
        p99_data = {"data": {"result": [
            {"metric": {"client": "a", "server": "b"}, "value": [0, "0.5"]},
        ]}}
        p50_data = {"data": {"result": [
            {"metric": {"client": "a", "server": "b"}, "value": [0, "0.05"]},
        ]}}
        edges = backend._parse_service_graph_results(count_data, error_data, p99_data, p50_data)
        assert len(edges) == 1
        assert edges[0].source_service == "a"
        assert edges[0].target_service == "b"
        assert edges[0].request_count == 100
        assert edges[0].error_count == 5
        assert edges[0].p99_latency_ms == 500.0  # 0.5s → 500ms
        assert edges[0].p50_latency_ms == 50.0   # 0.05s → 50ms

    def test_empty_results(self):
        backend = TempoTraceBackend()
        empty = {"data": {"result": []}}
        edges = backend._parse_service_graph_results(empty, empty, empty, empty)
        assert edges == []


class TestSpanToSummary:
    def test_error_span(self):
        backend = TempoTraceBackend()
        span = {
            "traceID": "abc123",
            "spanID": "def456",
            "rootServiceName": "fraud-detect",
            "rootTraceName": "POST /predict",
            "durationMs": 340,
            "status": "error",
            "span.status_message": "timeout waiting for model",
        }
        summary = backend._span_to_summary(span)
        assert summary.trace_id == "abc123"
        assert summary.status == "error"
        assert summary.error_message == "timeout waiting for model"

    def test_ok_span(self):
        backend = TempoTraceBackend()
        span = {
            "traceID": "xyz",
            "spanID": "uvw",
            "rootServiceName": "svc",
            "rootTraceName": "GET /health",
            "durationMs": 5,
        }
        summary = backend._span_to_summary(span)
        assert summary.status == "ok"
        assert summary.error_message is None


class TestServiceFilter:
    def test_single_service(self):
        result = TempoTraceBackend._traceql_service_filter(["fraud-detect"])
        assert result == 'resource.service.name = "fraud-detect"'

    def test_multiple_services(self):
        result = TempoTraceBackend._traceql_service_filter(["fraud-detect", "payment-api"])
        assert 'resource.service.name = "fraud-detect"' in result
        assert 'resource.service.name = "payment-api"' in result
        assert "||" in result


class TestParseTempoTimestamp:
    def test_nanoseconds_to_datetime(self):
        # 2026-04-10T10:00:00Z in nanoseconds
        nanos = 1_776_078_000_000_000_000
        result = TempoTraceBackend._parse_tempo_timestamp(nanos)
        assert result.year == 2026
        assert result.month == 4
        assert result.tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# Task 4: High-level query methods
# ---------------------------------------------------------------------------

from datetime import timedelta

from nthlayer_workers.correlate.traces.tempo import _ServiceStats


class TestQueryServiceStats:
    async def test_returns_service_stats(self):
        backend = TempoTraceBackend()
        call_count = 0

        async def mock_metrics(query, start, end, step):
            nonlocal call_count
            call_count += 1
            if "count_over_time" in query and "status = error" not in query:
                return {"series": [{
                    "labels": {"resource.service.name": "fraud-detect"},
                    "samples": [[1, 1204.0]],  # raw count
                }]}
            if "count_over_time" in query and "status = error" in query:
                return {"series": [{
                    "labels": {"resource.service.name": "fraud-detect"},
                    "samples": [[1, 24.0]],  # error count
                }]}
            # Latency queries return nanoseconds
            return {"series": [{
                "labels": {"resource.service.name": "fraud-detect"},
                "samples": [[1, 50_000_000.0]],  # 50ms in nanoseconds
            }]}

        backend._traceql_metrics_query = mock_metrics  # type: ignore
        start = datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 10, 10, 30, 0, tzinfo=timezone.utc)

        result = await backend._query_service_stats(["fraud-detect"], start, end)
        assert "fraud-detect" in result
        stats = result["fraud-detect"]
        assert stats.p50_ms == 50.0  # 50_000_000 ns / 1_000_000
        assert stats.total_count == 1204
        assert stats.error_count == 24
        assert call_count == 5  # p50, p95, p99, count, error

    async def test_skips_zero_count_service(self):
        backend = TempoTraceBackend()

        async def mock_metrics(query, start, end, step):
            if "count_over_time" in query and "status = error" not in query:
                return {"series": [{
                    "labels": {"resource.service.name": "ghost-svc"},
                    "samples": [[1, 0.0]],  # zero requests
                }]}
            return {"series": []}

        backend._traceql_metrics_query = mock_metrics  # type: ignore
        start = datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 10, 10, 30, 0, tzinfo=timezone.utc)
        result = await backend._query_service_stats(["ghost-svc"], start, end)
        assert "ghost-svc" not in result


class TestQueryOperationBreakdown:
    async def test_sorted_by_p99_desc_capped_at_10(self):
        backend = TempoTraceBackend()

        async def mock_metrics(query, start, end, step):
            # Return 12 operations to test the cap
            series = []
            for i in range(12):
                series.append({
                    "labels": {"resource.service.name": "svc", "name": f"op-{i}"},
                    "samples": [[1, float(i * 1_000_000)]],  # i ms in ns
                })
            return {"series": series}

        backend._traceql_metrics_query = mock_metrics  # type: ignore
        start = datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 10, 10, 30, 0, tzinfo=timezone.utc)

        result = await backend._query_operation_breakdown(
            ["svc"], start, end, start - timedelta(hours=1),
        )
        assert "svc" in result
        ops = result["svc"]
        assert len(ops) <= 10
        # Sorted by p99 descending
        p99_values = [op.p99_ms for op in ops]
        assert p99_values == sorted(p99_values, reverse=True)

    async def test_baseline_comparison(self):
        backend = TempoTraceBackend()
        call_count = 0

        async def mock_metrics(query, start, end, step):
            nonlocal call_count
            call_count += 1
            # 5th call is baseline p50
            if call_count == 5:
                return {"series": [{
                    "labels": {"resource.service.name": "svc", "name": "op"},
                    "samples": [[1, 50_000_000.0]],  # 50ms baseline
                }]}
            return {"series": [{
                "labels": {"resource.service.name": "svc", "name": "op"},
                "samples": [[1, 150_000_000.0]],  # 150ms incident
            }]}

        backend._traceql_metrics_query = mock_metrics  # type: ignore
        start = datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 10, 10, 30, 0, tzinfo=timezone.utc)

        result = await backend._query_operation_breakdown(
            ["svc"], start, end, start - timedelta(hours=1),
        )
        ops = result["svc"]
        assert ops[0].baseline_p50_ms == 50.0
        assert ops[0].change_pct == pytest.approx(200.0)  # (150-50)/50 * 100


class TestQueryEdgesFromTraces:
    async def test_aggregates_client_side(self):
        """Client spans: resource.service.name = caller, span.peer.service = target."""
        backend = TempoTraceBackend()

        async def mock_search(query, start, end, limit=1000):
            return [
                {"rootServiceName": "root", "resource.service.name": "a",
                 "span.peer.service": "b", "durationMs": 10},
                {"rootServiceName": "root", "resource.service.name": "a",
                 "span.peer.service": "b", "durationMs": 20, "status": "error"},
            ]

        backend._traceql_search = mock_search  # type: ignore
        start = datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 10, 10, 30, 0, tzinfo=timezone.utc)

        edges = await backend._query_edges_from_traces(["a", "b"], start, end)
        assert len(edges) == 1
        assert edges[0].source_service == "a"
        assert edges[0].target_service == "b"
        assert edges[0].request_count == 2
        assert edges[0].error_count == 1

    async def test_skips_self_calls(self):
        backend = TempoTraceBackend()

        async def mock_search(query, start, end, limit=1000):
            return [
                {"rootServiceName": "a", "resource.service.name": "a",
                 "span.peer.service": "a", "durationMs": 5},
            ]

        backend._traceql_search = mock_search  # type: ignore
        start = datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 10, 10, 30, 0, tzinfo=timezone.utc)

        edges = await backend._query_edges_from_traces(["a"], start, end)
        assert edges == []

    async def test_skips_missing_peer_service(self):
        """Spans without span.peer.service are skipped — can't determine target."""
        backend = TempoTraceBackend()

        async def mock_search(query, start, end, limit=1000):
            return [
                {"rootServiceName": "root", "resource.service.name": "a", "durationMs": 10},
            ]

        backend._traceql_search = mock_search  # type: ignore
        start = datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 10, 10, 30, 0, tzinfo=timezone.utc)

        edges = await backend._query_edges_from_traces(["a"], start, end)
        assert edges == []


class TestQueryServiceGraphsFromPrometheus:
    async def test_queries_prometheus(self):
        backend = TempoTraceBackend(prometheus_url="http://prom:9090")

        async def mock_prom(query, at):
            if "request_total" in query:
                return {"data": {"result": [
                    {"metric": {"client": "x", "server": "y"}, "value": [0, "50"]},
                ]}}
            if "request_failed" in query:
                return {"data": {"result": [
                    {"metric": {"client": "x", "server": "y"}, "value": [0, "2"]},
                ]}}
            if "0.99" in query:
                return {"data": {"result": [
                    {"metric": {"client": "x", "server": "y"}, "value": [0, "0.1"]},
                ]}}
            # p50
            return {"data": {"result": [
                {"metric": {"client": "x", "server": "y"}, "value": [0, "0.01"]},
            ]}}

        backend._prometheus_query = mock_prom  # type: ignore
        start = datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 10, 10, 30, 0, tzinfo=timezone.utc)

        edges = await backend._query_service_graphs_from_prometheus(["x", "y"], start, end)
        assert len(edges) == 1
        assert edges[0].request_count == 50
        assert edges[0].error_count == 2
        assert edges[0].p99_latency_ms == 100.0  # 0.1s → 100ms


class TestQueryTopErrors:
    async def test_groups_by_message_top_5(self):
        backend = TempoTraceBackend()

        async def mock_search(query, start, end, limit=100):
            spans = []
            for i in range(10):
                msg = f"error-{i % 6}"  # 6 distinct messages, so top 5 returned
                spans.append({
                    "span.status_message": msg,
                    "startTimeUnixNano": 1_776_078_000_000_000_000 + i,
                    "traceID": f"trace-{i}",
                })
            return spans

        backend._traceql_search = mock_search  # type: ignore
        start = datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 10, 10, 30, 0, tzinfo=timezone.utc)

        result = await backend._query_top_errors(["svc"], start, end)
        assert "svc" in result
        assert len(result["svc"]) <= 5


class TestQuerySampleTraces:
    async def test_limits_to_3(self):
        backend = TempoTraceBackend()

        async def mock_search(query, start, end, limit=20):
            # Respect the limit param like the real API would
            spans = [
                {"traceID": f"t{i}", "spanID": f"s{i}", "rootServiceName": "svc",
                 "rootTraceName": "op", "durationMs": i * 100, "durationNanos": i * 100_000_000}
                for i in range(10)
            ]
            return spans[:limit]

        backend._traceql_search = mock_search  # type: ignore
        start = datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 10, 10, 30, 0, tzinfo=timezone.utc)

        result = await backend._query_sample_traces(["svc"], start, end)
        assert "svc" in result
        assert len(result["svc"]["errors"]) <= 3
        assert len(result["svc"]["slow"]) <= 3


class TestGetTraceEvidence:
    async def test_assembles_full_evidence(self):
        backend = TempoTraceBackend(prometheus_url="http://prom:9090")

        async def mock_stats(services, start, end):
            return {"fraud-detect": _ServiceStats(
                p50_ms=50.0, p95_ms=120.0, p99_ms=340.0,
                total_count=1204, error_count=24, error_rate=0.02,
            )}

        async def mock_ops(services, start, end, baseline_start):
            return {}

        async def mock_edges_prom(services, start, end):
            return []

        async def mock_errors(services, start, end):
            return {}

        async def mock_samples(services, start, end):
            return {}

        backend._query_service_stats = mock_stats  # type: ignore
        backend._query_operation_breakdown = mock_ops  # type: ignore
        backend._query_service_graphs_from_prometheus = mock_edges_prom  # type: ignore
        backend._query_top_errors = mock_errors  # type: ignore
        backend._query_sample_traces = mock_samples  # type: ignore

        start = datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 10, 10, 30, 0, tzinfo=timezone.utc)

        evidence = await backend.get_trace_evidence(["fraud-detect"], start, end)
        assert evidence.backend == "tempo"
        assert evidence.query_time_ms > 0
        assert evidence.topology_divergence is None
        assert len(evidence.services) == 1
        assert evidence.services[0].service == "fraud-detect"
        assert evidence.services[0].p99_latency_ms == 340.0

    async def test_uses_service_graphs_when_available(self):
        backend = TempoTraceBackend(
            prometheus_url="http://prom:9090",
            use_service_graphs=True,
        )
        prom_called = False
        trace_search_called = False

        async def mock_stats(services, start, end):
            return {}

        async def mock_ops(services, start, end, baseline_start):
            return {}

        async def mock_edges_prom(services, start, end):
            nonlocal prom_called
            prom_called = True
            return []

        async def mock_edges_traces(services, start, end):
            nonlocal trace_search_called
            trace_search_called = True
            return []

        async def mock_errors(services, start, end):
            return {}

        async def mock_samples(services, start, end):
            return {}

        backend._query_service_stats = mock_stats  # type: ignore
        backend._query_operation_breakdown = mock_ops  # type: ignore
        backend._query_service_graphs_from_prometheus = mock_edges_prom  # type: ignore
        backend._query_edges_from_traces = mock_edges_traces  # type: ignore
        backend._query_top_errors = mock_errors  # type: ignore
        backend._query_sample_traces = mock_samples  # type: ignore

        start = datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 10, 10, 30, 0, tzinfo=timezone.utc)
        await backend.get_trace_evidence(["svc"], start, end)
        assert prom_called is True
        assert trace_search_called is False

    async def test_falls_back_to_trace_search(self):
        backend = TempoTraceBackend(use_service_graphs=False)
        trace_search_called = False

        async def mock_stats(services, start, end):
            return {}

        async def mock_ops(services, start, end, baseline_start):
            return {}

        async def mock_edges_traces(services, start, end):
            nonlocal trace_search_called
            trace_search_called = True
            return []

        async def mock_errors(services, start, end):
            return {}

        async def mock_samples(services, start, end):
            return {}

        backend._query_service_stats = mock_stats  # type: ignore
        backend._query_operation_breakdown = mock_ops  # type: ignore
        backend._query_edges_from_traces = mock_edges_traces  # type: ignore
        backend._query_top_errors = mock_errors  # type: ignore
        backend._query_sample_traces = mock_samples  # type: ignore

        start = datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 10, 10, 30, 0, tzinfo=timezone.utc)
        await backend.get_trace_evidence(["svc"], start, end)
        assert trace_search_called is True


class TestGetServiceDependencies:
    async def test_delegates_to_service_graphs(self):
        backend = TempoTraceBackend(
            prometheus_url="http://prom:9090",
            use_service_graphs=True,
        )
        called_with = {}

        async def mock_edges_prom(services, start, end):
            called_with["services"] = services
            return []

        backend._query_service_graphs_from_prometheus = mock_edges_prom  # type: ignore
        start = datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 10, 10, 30, 0, tzinfo=timezone.utc)

        await backend.get_service_dependencies("fraud-detect", start, end)
        assert called_with["services"] == ["fraud-detect"]

    async def test_delegates_to_trace_search(self):
        backend = TempoTraceBackend(use_service_graphs=False)
        called = False

        async def mock_edges_traces(services, start, end):
            nonlocal called
            called = True
            return []

        backend._query_edges_from_traces = mock_edges_traces  # type: ignore
        start = datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 10, 10, 30, 0, tzinfo=timezone.utc)

        await backend.get_service_dependencies("svc", start, end)
        assert called is True
