"""Topology-aware grouping. Deterministic transport."""
from __future__ import annotations

from nthlayer_workers.correlate.types import TemporalGroup, TopologyCorrelation


def _get_tier_rank(topology: dict, service: str) -> int:
    """Return numeric rank for service tier. Higher = more critical."""
    tier_ranks = {"critical": 3, "standard": 2, "low": 1}
    svc_info = topology.get(service, {})
    tier = svc_info.get("tier", "standard")
    return tier_ranks.get(tier, 1)


def _depends_on(topology: dict, service_a: str, service_b: str) -> bool:
    """Check if service_a depends on service_b."""
    svc_info = topology.get(service_a, {})
    return service_b in svc_info.get("dependencies", [])


def group_topology(
    temporal_groups: list[TemporalGroup],
    topology: dict | None,
) -> list[TopologyCorrelation]:
    """Link temporal groups for services that have dependency relationships.

    Args:
        temporal_groups: Groups from temporal.group_temporal()
        topology: Dict mapping service names to {dependencies: [...], dependents: [...], tier: ...}
                  Typically from OpenSRM manifests. None = skip topology grouping.

    Returns:
        List of TopologyCorrelations linking related service groups.
    """
    if topology is None:
        return []

    if len(temporal_groups) < 2:
        return []

    # Track which pairs have been linked to avoid duplicates
    linked: set[tuple[str, str]] = set()
    results: list[TopologyCorrelation] = []

    for i, group_a in enumerate(temporal_groups):
        for j, group_b in enumerate(temporal_groups):
            if i >= j:
                continue
            if group_a.service == group_b.service:
                continue

            pair_key = tuple(sorted((group_a.service, group_b.service)))
            if pair_key in linked:
                continue

            a_depends_on_b = _depends_on(topology, group_a.service, group_b.service)
            b_depends_on_a = _depends_on(topology, group_b.service, group_a.service)

            if not a_depends_on_b and not b_depends_on_a:
                continue

            linked.add(pair_key)

            # Determine primary: higher severity wins, then higher tier
            a_severity = group_a.peak_severity
            b_severity = group_b.peak_severity
            a_tier = _get_tier_rank(topology, group_a.service)
            b_tier = _get_tier_rank(topology, group_b.service)

            if a_severity > b_severity or (a_severity == b_severity and a_tier >= b_tier):
                primary_service = group_a.service
                secondary_service = group_b.service
                secondary_group = group_b
            else:
                primary_service = group_b.service
                secondary_service = group_a.service
                secondary_group = group_a

            # Determine relationship: from primary's perspective toward secondary
            if _depends_on(topology, primary_service, secondary_service):
                relationship = "depends_on"
            else:
                relationship = "depended_by"

            related_services = [
                {
                    "service": secondary_service,
                    "relationship": relationship,
                    "events": secondary_group.events,
                }
            ]

            # Build topology path
            topology_path = [primary_service, secondary_service]

            results.append(
                TopologyCorrelation(
                    primary_service=primary_service,
                    related_services=related_services,
                    topology_path=topology_path,
                )
            )

    return results
