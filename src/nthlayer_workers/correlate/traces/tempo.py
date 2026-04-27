"""Grafana Tempo trace backend adapter.

Uses TraceQL metrics API for server-side aggregation and TraceQL search
for sample traces. Optionally queries Prometheus for pre-computed service
graph metrics (metrics-generator).
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

from .protocol import (
    ErrorSummary,
    OperationLatency,
    ServiceCallEdge,
    ServiceTraceProfile,
    TraceEvidence,
    TraceSpanSummary,
)


class TempoTraceBackend:
    """TraceBackend implementation for Grafana Tempo."""

    def __init__(
        self,
        endpoint: str | None = None,
        org_id: str | None = None,
        timeout: int = 30,
        use_service_graphs: bool = True,
        prometheus_url: str | None = None,
    ):
        self.endpoint = (
            endpoint
            or os.environ.get("TEMPO_ENDPOINT", "http://localhost:3200")
        )
        self.org_id = org_id or os.environ.get("TEMPO_ORG_ID", "")
        self.timeout = timeout
        self.use_service_graphs = use_service_graphs
        self.prometheus_url = prometheus_url

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.org_id:
            headers["X-Scope-OrgID"] = self.org_id

        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers=headers,
        )

    # -------------------------------------------------------------------
    # TraceBackend protocol methods
    # -------------------------------------------------------------------

    async def get_trace_evidence(
        self,
        services: list[str],
        start: datetime,
        end: datetime,
        baseline_window: timedelta = timedelta(hours=1),
    ) -> TraceEvidence:
        query_start = time.monotonic()
        baseline_start = start - baseline_window

        # Phase 1: Per-service latency + error aggregation
        incident_stats = await self._query_service_stats(services, start, end)
        baseline_stats = await self._query_service_stats(services, baseline_start, start)

        # Phase 2: Service-to-service call edges
        if self.use_service_graphs and self.prometheus_url:
            edges = await self._query_service_graphs_from_prometheus(services, start, end)
        else:
            edges = await self._query_edges_from_traces(services, start, end)

        # Phase 3: Per-operation breakdown
        operations = await self._query_operation_breakdown(
            services, start, end, baseline_start,
        )

        # Phase 4: Top errors
        errors = await self._query_top_errors(services, start, end)

        # Phase 5: Sample traces
        samples = await self._query_sample_traces(services, start, end)

        # Assemble per-service profiles
        service_profiles: list[ServiceTraceProfile] = []
        for service in services:
            incident = incident_stats.get(service)
            baseline = baseline_stats.get(service)

            if not incident:
                continue

            latency_change = None
            if baseline and baseline.p50_ms > 0:
                latency_change = (
                    (incident.p50_ms - baseline.p50_ms) / baseline.p50_ms * 100
                )

            profile = ServiceTraceProfile(
                service=service,
                time_window_start=start,
                time_window_end=end,
                callers=[e for e in edges if e.target_service == service],
                callees=[e for e in edges if e.source_service == service],
                p50_latency_ms=incident.p50_ms,
                p95_latency_ms=incident.p95_ms,
                p99_latency_ms=incident.p99_ms,
                baseline_p50_ms=baseline.p50_ms if baseline else None,
                latency_change_pct=latency_change,
                error_rate=incident.error_rate,
                error_count=incident.error_count,
                total_request_count=incident.total_count,
                top_errors=errors.get(service, []),
                slow_operations=operations.get(service, []),
                sample_error_traces=samples.get(service, {}).get("errors", []),
                sample_slow_traces=samples.get(service, {}).get("slow", []),
            )
            service_profiles.append(profile)

        query_time = (time.monotonic() - query_start) * 1000

        return TraceEvidence(
            services=service_profiles,
            topology_divergence=None,  # computed by caller with specs
            query_time_ms=query_time,
            backend="tempo",
        )

    async def get_service_dependencies(
        self,
        service: str,
        start: datetime,
        end: datetime,
    ) -> list[ServiceCallEdge]:
        if self.use_service_graphs and self.prometheus_url:
            return await self._query_service_graphs_from_prometheus(
                [service], start, end,
            )
        return await self._query_edges_from_traces([service], start, end)

    async def health_check(self) -> bool:
        try:
            response = await self._client.get(f"{self.endpoint}/ready")
            return response.status_code == 200
        except Exception:
            return False

    async def aclose(self) -> None:
        """Close the underlying httpx client and release connections."""
        await self._client.aclose()

    # -------------------------------------------------------------------
    # High-level query methods
    # -------------------------------------------------------------------

    async def _query_service_stats(
        self, services: list[str], start: datetime, end: datetime,
    ) -> dict[str, _ServiceStats]:
        """Per-service latency percentiles, error count, and request count."""

        service_filter = self._traceql_service_filter(services)
        duration_secs = max(int((end - start).total_seconds()), 60)
        step = f"{max(duration_secs // 20, 15)}s"

        p50_query = (
            f'{{ {service_filter} && span.kind = server }} '
            f'| quantile_over_time(duration, .50) by (resource.service.name)'
        )
        p95_query = (
            f'{{ {service_filter} && span.kind = server }} '
            f'| quantile_over_time(duration, .95) by (resource.service.name)'
        )
        p99_query = (
            f'{{ {service_filter} && span.kind = server }} '
            f'| quantile_over_time(duration, .99) by (resource.service.name)'
        )
        count_query = (
            f'{{ {service_filter} && span.kind = server }} '
            f'| count_over_time() by (resource.service.name)'
        )
        error_query = (
            f'{{ {service_filter} && span.kind = server && status = error }} '
            f'| count_over_time() by (resource.service.name)'
        )

        p50_data, p95_data, p99_data, count_data, error_data = await asyncio.gather(
            self._traceql_metrics_query(p50_query, start, end, step),
            self._traceql_metrics_query(p95_query, start, end, step),
            self._traceql_metrics_query(p99_query, start, end, step),
            self._traceql_metrics_query(count_query, start, end, step),
            self._traceql_metrics_query(error_query, start, end, step),
        )

        p50_by_svc = self._extract_metric_by_service(p50_data)
        p95_by_svc = self._extract_metric_by_service(p95_data)
        p99_by_svc = self._extract_metric_by_service(p99_data)
        count_by_svc = self._extract_metric_by_service(count_data)
        error_by_svc = self._extract_metric_by_service(error_data)

        results: dict[str, _ServiceStats] = {}
        for service in services:
            total = count_by_svc.get(service, 0)
            errors = error_by_svc.get(service, 0)
            if total == 0:
                continue
            results[service] = _ServiceStats(
                p50_ms=p50_by_svc.get(service, 0) / 1_000_000,
                p95_ms=p95_by_svc.get(service, 0) / 1_000_000,
                p99_ms=p99_by_svc.get(service, 0) / 1_000_000,
                total_count=int(total),
                error_count=int(errors),
                error_rate=errors / total if total > 0 else 0.0,
            )
        return results

    async def _query_operation_breakdown(
        self,
        services: list[str],
        start: datetime,
        end: datetime,
        baseline_start: datetime,
    ) -> dict[str, list[OperationLatency]]:
        """Per-operation latency breakdown within each service."""

        service_filter = self._traceql_service_filter(services)
        duration_secs = max(int((end - start).total_seconds()), 60)
        step = f"{max(duration_secs // 20, 15)}s"

        p50_query = (
            f'{{ {service_filter} && span.kind = server }} '
            f'| quantile_over_time(duration, .50) by (resource.service.name, name)'
        )
        p99_query = (
            f'{{ {service_filter} && span.kind = server }} '
            f'| quantile_over_time(duration, .99) by (resource.service.name, name)'
        )
        count_query = (
            f'{{ {service_filter} && span.kind = server }} '
            f'| count_over_time() by (resource.service.name, name)'
        )
        error_query = (
            f'{{ {service_filter} && span.kind = server && status = error }} '
            f'| count_over_time() by (resource.service.name, name)'
        )

        (inc_p50, inc_p99, inc_count, inc_error, base_p50) = await asyncio.gather(
            self._traceql_metrics_query(p50_query, start, end, step),
            self._traceql_metrics_query(p99_query, start, end, step),
            self._traceql_metrics_query(count_query, start, end, step),
            self._traceql_metrics_query(error_query, start, end, step),
            self._traceql_metrics_query(p50_query, baseline_start, start, step),
        )

        inc_p50_map = self._extract_metric_by_service_and_op(inc_p50)
        inc_p99_map = self._extract_metric_by_service_and_op(inc_p99)
        inc_count_map = self._extract_metric_by_service_and_op(inc_count)
        inc_error_map = self._extract_metric_by_service_and_op(inc_error)
        base_p50_map = self._extract_metric_by_service_and_op(base_p50)

        result: dict[str, list[OperationLatency]] = {}

        for (service, operation), p50_ns in inc_p50_map.items():
            if service not in result:
                result[service] = []

            total = inc_count_map.get((service, operation), 0)
            errors = inc_error_map.get((service, operation), 0)
            p50_ms = p50_ns / 1_000_000
            p99_ms = inc_p99_map.get((service, operation), 0) / 1_000_000
            baseline_p50_ns = base_p50_map.get((service, operation))
            baseline_p50_ms = baseline_p50_ns / 1_000_000 if baseline_p50_ns else None

            change_pct = None
            if baseline_p50_ms and baseline_p50_ms > 0:
                change_pct = (p50_ms - baseline_p50_ms) / baseline_p50_ms * 100

            result[service].append(OperationLatency(
                operation=operation,
                p50_ms=p50_ms,
                p99_ms=p99_ms,
                request_count=int(total),
                error_rate=errors / total if total > 0 else 0.0,
                baseline_p50_ms=baseline_p50_ms,
                change_pct=change_pct,
            ))

        for service in result:
            result[service].sort(key=lambda o: o.p99_ms, reverse=True)
            result[service] = result[service][:10]

        return result

    async def _query_edges_from_traces(
        self, services: list[str], start: datetime, end: datetime,
    ) -> list[ServiceCallEdge]:
        """Derive service-to-service edges from trace search (slow path).

        For client spans: resource.service.name is the calling service,
        span.peer.service is the target. Less accurate than service graph
        metrics — use_service_graphs=True is preferred when available.
        """
        service_filter = self._traceql_service_filter(services)
        search_query = f'{{ {service_filter} && span.kind = client }}'

        results = await self._traceql_search(search_query, start, end, limit=1000)

        edge_counts: dict[tuple[str, str], _EdgeAccumulator] = {}

        for span in results:
            source = span.get("resource.service.name", "")
            target = span.get("span.peer.service", "")
            if not source or not target or source == target:
                continue

            key = (source, target)
            if key not in edge_counts:
                edge_counts[key] = _EdgeAccumulator()

            acc = edge_counts[key]
            acc.count += 1
            duration_ms = span.get("durationMs", 0)
            acc.durations.append(duration_ms)
            if span.get("status") == "error":
                acc.errors += 1

        edges = []
        for (source, target), acc in edge_counts.items():
            sorted_d = sorted(acc.durations)
            edges.append(ServiceCallEdge(
                source_service=source,
                target_service=target,
                request_count=acc.count,
                error_count=acc.errors,
                p50_latency_ms=sorted_d[len(sorted_d) // 2] if sorted_d else 0,
                p99_latency_ms=sorted_d[int(len(sorted_d) * 0.99)] if sorted_d else 0,
            ))
        return edges

    async def _query_service_graphs_from_prometheus(
        self, services: list[str], start: datetime, end: datetime,
    ) -> list[ServiceCallEdge]:
        """Fast path: query pre-computed service graph metrics from Prometheus."""

        if not self.prometheus_url or not services:
            return []

        service_regex = "|".join(services)
        window = f"{int((end - start).total_seconds())}s"

        count_query = (
            f'sum by (client, server) ('
            f'increase(traces_service_graph_request_total'
            f'{{client=~"{service_regex}|", server=~"|{service_regex}"}}'
            f'[{window}]))'
        )
        error_query = (
            f'sum by (client, server) ('
            f'increase(traces_service_graph_request_failed_total'
            f'{{client=~"{service_regex}|", server=~"|{service_regex}"}}'
            f'[{window}]))'
        )
        p99_query = (
            f'histogram_quantile(0.99, sum by (client, server, le) ('
            f'increase(traces_service_graph_request_server_seconds_bucket'
            f'{{client=~"{service_regex}|", server=~"|{service_regex}"}}'
            f'[{window}])))'
        )
        p50_query = p99_query.replace("0.99", "0.50")

        count_data, error_data, p99_data, p50_data = await asyncio.gather(
            self._prometheus_query(count_query, end),
            self._prometheus_query(error_query, end),
            self._prometheus_query(p99_query, end),
            self._prometheus_query(p50_query, end),
        )

        return self._parse_service_graph_results(
            count_data, error_data, p99_data, p50_data,
        )

    async def _query_top_errors(
        self, services: list[str], start: datetime, end: datetime,
    ) -> dict[str, list[ErrorSummary]]:
        """Top error messages per service (parallel across services)."""

        async def _fetch_one(service: str) -> tuple[str, list[ErrorSummary]]:
            search_query = (
                f'{{ resource.service.name = "{service}" '
                f'&& status = error && span.kind = server }}'
            )
            spans = await self._traceql_search(search_query, start, end, limit=100)
            if not spans:
                return service, []

            error_groups: dict[str, list[dict]] = {}
            for span in spans:
                msg = (
                    span.get("span.status_message", "")
                    or span.get("status.message", "")
                    or "unknown error"
                )
                error_groups.setdefault(msg, []).append(span)

            sorted_errors = sorted(
                error_groups.items(), key=lambda x: len(x[1]), reverse=True,
            )[:5]

            summaries = []
            for msg, group_spans in sorted_errors:
                timestamps = [s.get("startTimeUnixNano", 0) for s in group_spans
                              if s.get("startTimeUnixNano", 0) > 0]
                first = self._parse_tempo_timestamp(min(timestamps)) if timestamps else None
                last = self._parse_tempo_timestamp(max(timestamps)) if timestamps else None
                summaries.append(ErrorSummary(
                    error_message=msg,
                    count=len(group_spans),
                    first_seen=first,
                    last_seen=last,
                    sample_trace_id=group_spans[0].get("traceID"),
                ))
            return service, summaries

        pairs = await asyncio.gather(*[_fetch_one(s) for s in services])
        return {svc: errs for svc, errs in pairs if errs}

    async def _query_sample_traces(
        self, services: list[str], start: datetime, end: datetime,
    ) -> dict[str, dict[str, list[TraceSpanSummary]]]:
        """Sample error and slow traces per service (parallel across services)."""

        async def _fetch_one(service: str) -> tuple[str, dict[str, list[TraceSpanSummary]]]:
            error_query = (
                f'{{ resource.service.name = "{service}" '
                f'&& status = error && span.kind = server }}'
            )
            error_spans = await self._traceql_search(error_query, start, end, limit=3)

            slow_query = (
                f'{{ resource.service.name = "{service}" '
                f'&& span.kind = server }}'
            )
            slow_spans_raw = await self._traceql_search(slow_query, start, end, limit=50)
            slow_spans_raw.sort(
                key=lambda s: s.get("durationNanos", 0), reverse=True,
            )
            slow_spans = slow_spans_raw[:3]

            return service, {
                "errors": [self._span_to_summary(s) for s in error_spans],
                "slow": [self._span_to_summary(s) for s in slow_spans],
            }

        pairs = await asyncio.gather(*[_fetch_one(s) for s in services])
        return {svc: data for svc, data in pairs}

    # -------------------------------------------------------------------
    # TraceQL Metrics API
    # -------------------------------------------------------------------

    async def _traceql_metrics_query(
        self,
        query: str,
        start: datetime,
        end: datetime,
        step: str,
    ) -> dict:
        params = {
            "q": query,
            "start": str(int(start.timestamp())),
            "end": str(int(end.timestamp())),
            "step": step,
        }
        response = await self._client.get(
            f"{self.endpoint}/api/metrics/query_range",
            params=params,
        )
        response.raise_for_status()
        return response.json()

    async def _traceql_search(
        self,
        query: str,
        start: datetime,
        end: datetime,
        limit: int = 20,
    ) -> list[dict]:
        params = {
            "q": query,
            "start": str(int(start.timestamp())),
            "end": str(int(end.timestamp())),
            "limit": str(limit),
        }
        response = await self._client.get(
            f"{self.endpoint}/api/search",
            params=params,
        )
        response.raise_for_status()
        data = response.json()
        return data.get("traces", [])

    async def _prometheus_query(self, query: str, at: datetime) -> dict:
        if not self.prometheus_url:
            return {}
        params = {
            "query": query,
            "time": str(int(at.timestamp())),
        }
        response = await self._client.get(
            f"{self.prometheus_url}/api/v1/query",
            params=params,
        )
        response.raise_for_status()
        return response.json()

    # -------------------------------------------------------------------
    # Result parsing helpers (Task 3)
    # -------------------------------------------------------------------

    def _extract_metric_by_service(self, response: dict) -> dict[str, float]:
        """Extract {service_name: average_value} from TraceQL metrics response."""
        result: dict[str, float] = {}
        for series in response.get("series", []):
            service = (
                series.get("labels", {}).get("resource.service.name", "")
                or series.get("promLabels", {}).get("resource_service_name", "")
            )
            if not service:
                continue
            samples = series.get("samples", [])
            if samples:
                values = [s[1] for s in samples if len(s) >= 2 and s[1] is not None]
                result[service] = sum(values) / len(values) if values else 0
        return result

    def _extract_metric_by_service_and_op(
        self, response: dict,
    ) -> dict[tuple[str, str], float]:
        """Extract {(service, operation): average_value} from faceted response."""
        result: dict[tuple[str, str], float] = {}
        for series in response.get("series", []):
            labels = series.get("labels", {})
            service = labels.get("resource.service.name", "")
            operation = labels.get("name", "")
            if not service or not operation:
                continue
            samples = series.get("samples", [])
            if samples:
                values = [s[1] for s in samples if len(s) >= 2 and s[1] is not None]
                result[(service, operation)] = (
                    sum(values) / len(values) if values else 0
                )
        return result

    def _parse_service_graph_results(
        self,
        count_data: dict,
        error_data: dict,
        p99_data: dict,
        p50_data: dict,
    ) -> list[ServiceCallEdge]:
        """Parse Prometheus service graph metrics into ServiceCallEdge objects."""
        edges: dict[tuple[str, str], dict] = {}

        for result in count_data.get("data", {}).get("result", []):
            client = result["metric"].get("client", "")
            server = result["metric"].get("server", "")
            if client and server:
                edges[(client, server)] = {
                    "count": float(result["value"][1]),
                    "errors": 0, "p50": 0, "p99": 0,
                }

        for result in error_data.get("data", {}).get("result", []):
            key = (
                result["metric"].get("client", ""),
                result["metric"].get("server", ""),
            )
            if key in edges:
                edges[key]["errors"] = float(result["value"][1])

        for result in p99_data.get("data", {}).get("result", []):
            key = (
                result["metric"].get("client", ""),
                result["metric"].get("server", ""),
            )
            if key in edges:
                edges[key]["p99"] = float(result["value"][1]) * 1000

        for result in p50_data.get("data", {}).get("result", []):
            key = (
                result["metric"].get("client", ""),
                result["metric"].get("server", ""),
            )
            if key in edges:
                edges[key]["p50"] = float(result["value"][1]) * 1000

        return [
            ServiceCallEdge(
                source_service=client,
                target_service=server,
                request_count=int(vals["count"]),
                error_count=int(vals["errors"]),
                p50_latency_ms=vals["p50"],
                p99_latency_ms=vals["p99"],
            )
            for (client, server), vals in edges.items()
        ]

    def _span_to_summary(self, span: dict) -> TraceSpanSummary:
        """Convert a Tempo search result span to TraceSpanSummary."""
        return TraceSpanSummary(
            trace_id=span.get("traceID", ""),
            span_id=span.get("spanID", ""),
            service=span.get("rootServiceName", ""),
            operation=span.get("rootTraceName", ""),
            duration_ms=span.get("durationMs", 0),
            status="error" if span.get("status") == "error" else "ok",
            error_message=span.get("span.status_message"),
            parent_service=None,
            attributes={},
        )

    @staticmethod
    def _parse_tempo_timestamp(nanos: int) -> datetime:
        """Convert nanosecond unix timestamp to datetime."""
        return datetime.fromtimestamp(nanos / 1_000_000_000, tz=timezone.utc)

    @staticmethod
    def _traceql_service_filter(services: list[str]) -> str:
        """Build a TraceQL filter for multiple services."""
        if not services:
            return 'resource.service.name = "__none__"'
        if len(services) == 1:
            return f'resource.service.name = "{services[0]}"'
        conditions = " || ".join(
            f'resource.service.name = "{s}"' for s in services
        )
        return f"({conditions})"


@dataclass
class _ServiceStats:
    """Internal: aggregated stats for a single service."""

    p50_ms: float
    p95_ms: float
    p99_ms: float
    total_count: int
    error_count: int
    error_rate: float


@dataclass
class _EdgeAccumulator:
    """Internal: accumulator for client-side edge aggregation."""

    count: int = 0
    errors: int = 0
    durations: list[float] = field(default_factory=list)
