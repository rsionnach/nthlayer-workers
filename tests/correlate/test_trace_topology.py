"""Tests for topology divergence detection (declared vs observed from traces)."""

from __future__ import annotations

from datetime import datetime, timezone

from nthlayer_workers.correlate.traces.protocol import (
    ServiceCallEdge,
    ServiceTraceProfile,
    TraceEvidence,
)
from nthlayer_workers.correlate.traces.topology import detect_topology_divergence


def _make_profile(service: str, callees: list[tuple[str, str]] | None = None,
                  callers: list[tuple[str, str]] | None = None) -> ServiceTraceProfile:
    """Build a minimal ServiceTraceProfile with caller/callee edges."""
    now = datetime.now(tz=timezone.utc)
    callee_edges = [
        ServiceCallEdge(source_service=src, target_service=tgt,
                        request_count=100, error_count=0,
                        p50_latency_ms=10, p99_latency_ms=50)
        for src, tgt in (callees or [])
    ]
    caller_edges = [
        ServiceCallEdge(source_service=src, target_service=tgt,
                        request_count=100, error_count=0,
                        p50_latency_ms=10, p99_latency_ms=50)
        for src, tgt in (callers or [])
    ]
    return ServiceTraceProfile(
        service=service,
        time_window_start=now, time_window_end=now,
        callers=caller_edges, callees=callee_edges,
        p50_latency_ms=10, p95_latency_ms=30, p99_latency_ms=50,
        baseline_p50_ms=None, latency_change_pct=None,
        error_rate=0.0, error_count=0, total_request_count=100,
        top_errors=[], slow_operations=[],
        sample_error_traces=[], sample_slow_traces=[],
    )


def _make_evidence(profiles: list[ServiceTraceProfile]) -> TraceEvidence:
    return TraceEvidence(
        services=profiles,
        topology_divergence=None,
        query_time_ms=100.0,
        backend="tempo",
    )


class TestDetectTopologyDivergence:
    def test_no_divergence(self):
        """Declared A→B matches observed A→B."""
        evidence = _make_evidence([
            _make_profile("A", callees=[("A", "B")]),
            _make_profile("B"),
        ])
        declared = {
            "A": {"tier": "critical", "dependencies": ["B"], "dependents": []},
            "B": {"tier": "standard", "dependencies": [], "dependents": ["A"]},
        }
        div = detect_topology_divergence(evidence, declared)
        assert div.declared_not_observed == []
        assert div.observed_not_declared == []

    def test_declared_not_observed(self):
        """Declared A→B but no trace edge."""
        evidence = _make_evidence([
            _make_profile("A"),
            _make_profile("B"),
        ])
        declared = {
            "A": {"tier": "critical", "dependencies": ["B"], "dependents": []},
            "B": {"tier": "standard", "dependencies": [], "dependents": ["A"]},
        }
        div = detect_topology_divergence(evidence, declared)
        assert ("A", "B") in div.declared_not_observed

    def test_observed_not_declared(self):
        """Traces show A→C but not in specs."""
        evidence = _make_evidence([
            _make_profile("A", callees=[("A", "C")]),
            _make_profile("C"),
        ])
        declared = {
            "A": {"tier": "critical", "dependencies": [], "dependents": []},
            "C": {"tier": "standard", "dependencies": [], "dependents": []},
        }
        div = detect_topology_divergence(evidence, declared)
        assert ("A", "C") in div.observed_not_declared

    def test_mixed_divergence(self):
        """Some match, some only declared, some only observed."""
        evidence = _make_evidence([
            _make_profile("A", callees=[("A", "B"), ("A", "D")]),
            _make_profile("B"),
            _make_profile("D"),
        ])
        declared = {
            "A": {"tier": "critical", "dependencies": ["B", "C"], "dependents": []},
            "B": {"tier": "standard", "dependencies": [], "dependents": ["A"]},
            "C": {"tier": "standard", "dependencies": [], "dependents": ["A"]},
            "D": {"tier": "standard", "dependencies": [], "dependents": []},
        }
        div = detect_topology_divergence(evidence, declared)
        # A→C declared but not observed (C not in blast radius profiles, but is in declared_deps)
        # Actually C is not in blast radius — only A, B, D have profiles
        # So A→C should NOT appear because C is not in blast_services
        assert ("A", "D") in div.observed_not_declared  # observed but not declared
        assert ("A", "B") not in div.declared_not_observed  # matches
        assert ("A", "B") not in div.observed_not_declared  # matches

    def test_only_blast_radius_compared(self):
        """Declared edge to service outside blast radius is ignored."""
        evidence = _make_evidence([
            _make_profile("A"),
        ])
        declared = {
            "A": {"tier": "critical", "dependencies": ["Z"], "dependents": []},
            "Z": {"tier": "standard", "dependencies": [], "dependents": ["A"]},
        }
        div = detect_topology_divergence(evidence, declared)
        # Z not in trace evidence services, so A→Z is not compared
        assert div.declared_not_observed == []
        assert div.observed_not_declared == []

    def test_empty_trace_evidence(self):
        """No profiles → empty divergence."""
        evidence = _make_evidence([])
        declared = {
            "A": {"tier": "critical", "dependencies": ["B"], "dependents": []},
        }
        div = detect_topology_divergence(evidence, declared)
        assert div.declared_not_observed == []
        assert div.observed_not_declared == []
