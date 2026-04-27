"""Topology divergence detection: declared vs observed from traces."""

from __future__ import annotations

from .protocol import TopologyDivergence, TraceEvidence


def detect_topology_divergence(
    trace_evidence: TraceEvidence,
    declared_deps: dict[str, dict],
) -> TopologyDivergence:
    """Compare observed trace edges against declared dependency graph.

    Only compares edges where both endpoints are in the trace evidence
    (blast radius services). Edges to/from services outside the blast
    radius are ignored.
    """
    # Build set of observed edges from trace profiles
    observed_edges: set[tuple[str, str]] = set()
    for svc_profile in trace_evidence.services:
        for callee in svc_profile.callees:
            observed_edges.add((callee.source_service, callee.target_service))
        for caller in svc_profile.callers:
            observed_edges.add((caller.source_service, caller.target_service))

    # Build set of declared edges (only for services in blast radius)
    blast_services = {svc.service for svc in trace_evidence.services}
    declared_edges: set[tuple[str, str]] = set()
    for service, info in declared_deps.items():
        if service not in blast_services:
            continue
        for dep in info.get("dependencies", []):
            if dep in blast_services:
                declared_edges.add((service, dep))

    return TopologyDivergence(
        declared_not_observed=sorted(declared_edges - observed_edges),
        observed_not_declared=sorted(observed_edges - declared_edges),
    )
