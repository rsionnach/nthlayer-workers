"""Trace backend protocol and data types for correlation evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol


@dataclass
class TraceSpanSummary:
    """A single span from a trace, summarised for correlation evidence."""

    trace_id: str
    span_id: str
    service: str
    operation: str
    duration_ms: float
    status: str  # "ok" | "error" | "unset"
    error_message: str | None
    parent_service: str | None
    attributes: dict[str, str]


@dataclass
class ServiceCallEdge:
    """An observed call between two services, with latency and error stats."""

    source_service: str
    target_service: str
    request_count: int
    error_count: int
    p50_latency_ms: float
    p99_latency_ms: float


@dataclass
class ErrorSummary:
    """Top errors for a service during the incident window."""

    error_message: str
    count: int
    first_seen: datetime | None
    last_seen: datetime | None
    sample_trace_id: str | None


@dataclass
class OperationLatency:
    """Latency for a specific operation within a service."""

    operation: str
    p50_ms: float
    p99_ms: float
    request_count: int
    error_rate: float
    baseline_p50_ms: float | None
    change_pct: float | None


@dataclass
class TopologyDivergence:
    """Where observed trace topology differs from declared dependency graph."""

    declared_not_observed: list[tuple[str, str]]
    observed_not_declared: list[tuple[str, str]]


@dataclass
class ServiceTraceProfile:
    """Trace-derived evidence for a single service during the incident window."""

    service: str
    time_window_start: datetime
    time_window_end: datetime
    callers: list[ServiceCallEdge]
    callees: list[ServiceCallEdge]
    p50_latency_ms: float
    p95_latency_ms: float
    p99_latency_ms: float
    baseline_p50_ms: float | None
    latency_change_pct: float | None
    error_rate: float
    error_count: int
    total_request_count: int
    top_errors: list[ErrorSummary]
    slow_operations: list[OperationLatency]
    sample_error_traces: list[TraceSpanSummary]
    sample_slow_traces: list[TraceSpanSummary]


@dataclass
class TraceEvidence:
    """Complete trace evidence for all services in the blast radius."""

    services: list[ServiceTraceProfile]
    topology_divergence: TopologyDivergence | None
    query_time_ms: float
    backend: str  # "tempo" | "jaeger"


class TraceBackend(Protocol):
    """Protocol that all trace backend adapters implement."""

    async def get_trace_evidence(
        self,
        services: list[str],
        start: datetime,
        end: datetime,
        baseline_window: timedelta = timedelta(hours=1),
    ) -> TraceEvidence: ...

    async def get_service_dependencies(
        self,
        service: str,
        start: datetime,
        end: datetime,
    ) -> list[ServiceCallEdge]: ...

    async def health_check(self) -> bool: ...
