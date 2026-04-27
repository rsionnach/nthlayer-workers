"""Pre-correlation engine. Orchestrates all correlation steps."""
from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from nthlayer_workers.correlate.correlation.changes import find_change_candidates
from nthlayer_workers.correlate.correlation.dedup import deduplicate
from nthlayer_workers.correlate.correlation.temporal import group_temporal
from nthlayer_workers.correlate.correlation.topology import group_topology
from nthlayer_workers.correlate.ingestion.severity import pre_score
from nthlayer_workers.correlate.store.protocol import EventStore
from nthlayer_workers.correlate.types import ChangeCandidate, CorrelationGroup, TemporalGroup


class CorrelationEngine:
    """Orchestrates the full pre-correlation pipeline. All deterministic transport."""

    def correlate(
        self,
        store: EventStore,
        window_minutes: int = 5,
        topology: dict | None = None,
        slo_targets: dict | None = None,
    ) -> list[CorrelationGroup]:
        """Run full pre-correlation pipeline. All deterministic transport.

        Steps:
        1. Query store for events in window
        2. Deduplicate
        3. Severity enrichment (second-pass for events without SLO context)
        4. Temporal grouping
        5. Topology-aware grouping
        6. Change candidate indexing
        7. Assemble CorrelationGroups with priority scoring
        """
        # Step 1: Query store for events in window
        now = datetime.now(timezone.utc)
        start = (now - timedelta(minutes=window_minutes)).isoformat()
        end = now.isoformat()

        events = store.get_by_time_window(start, end)
        if not events:
            return []

        # Step 2: Deduplicate
        deduped = deduplicate(events)

        # Step 3: Severity enrichment
        enriched = []
        for event in deduped:
            new_severity = pre_score(event, slo_targets)
            if new_severity != event.severity:
                event = replace(event, severity=new_severity)
            enriched.append(event)

        # Step 4: Temporal grouping
        temporal_groups = group_temporal(enriched, window_minutes=window_minutes)
        if not temporal_groups:
            return []

        # Step 5: Topology-aware grouping
        topology_correlations = group_topology(temporal_groups, topology)

        # Step 6: Change candidate indexing
        change_candidates_map = find_change_candidates(
            store, temporal_groups, topology=topology, window_minutes=max(window_minutes, 30)
        )

        # Step 7: Assemble CorrelationGroups with priority scoring
        return self.assemble_groups(
            temporal_groups, topology_correlations, change_candidates_map, topology
        )

    def assemble_groups(
        self,
        temporal_groups: list[TemporalGroup],
        topology_correlations: list,
        change_candidates_map: dict[str, list[ChangeCandidate]],
        topology: dict | None,
    ) -> list[CorrelationGroup]:
        """Assemble CorrelationGroups from pre-computed correlation sub-step outputs.

        Two-pass assembly: topology-linked groups first, then unassigned temporal groups.
        Used by both correlate() and CLI replay.
        """
        assigned: set[int] = set()
        correlation_groups: list[CorrelationGroup] = []

        # First pass: create groups from topology correlations
        for tc in topology_correlations:
            involved_services = {tc.primary_service}
            for rs in tc.related_services:
                involved_services.add(rs["service"])

            signals: list[TemporalGroup] = []
            for gi, tg in enumerate(temporal_groups):
                if tg.service in involved_services and gi not in assigned:
                    signals.append(tg)
                    assigned.add(gi)

            if not signals:
                continue

            all_changes: list[ChangeCandidate] = []
            for svc in involved_services:
                all_changes.extend(change_candidates_map.get(svc, []))

            peak_severity = max(s.peak_severity for s in signals)
            priority = self._compute_priority(
                peak_severity, list(involved_services), topology, topology_correlations
            )

            total_events = sum(s.count for s in signals)
            primary_type = self._dominant_event_type(signals)
            services_str = ", ".join(sorted(involved_services))
            summary = (
                f"{total_events} {primary_type}(s) on {services_str} "
                f"with {len(all_changes)} recent change(s)"
            )

            all_timestamps = []
            for s in signals:
                all_timestamps.append(s.time_window[0])
                all_timestamps.append(s.time_window[1])
            first_seen = min(all_timestamps)
            last_updated = max(all_timestamps)

            group_id = f"cg-{uuid.uuid4().hex[:8]}"
            correlation_groups.append(
                CorrelationGroup(
                    id=group_id,
                    priority=priority,
                    summary=summary,
                    services=sorted(involved_services),
                    signals=signals,
                    topology=tc,
                    change_candidates=all_changes,
                    first_seen=first_seen,
                    last_updated=last_updated,
                    event_count=total_events,
                )
            )

        # Second pass: create groups for unassigned temporal groups
        for gi, tg in enumerate(temporal_groups):
            if gi in assigned:
                continue

            service = tg.service
            changes = change_candidates_map.get(service, [])
            peak_severity = tg.peak_severity
            priority = self._compute_priority(
                peak_severity, [service], topology, topology_correlations
            )

            primary_type = self._dominant_event_type([tg])
            summary = (
                f"{tg.count} {primary_type}(s) on {service} "
                f"with {len(changes)} recent change(s)"
            )

            group_id = f"cg-{uuid.uuid4().hex[:8]}"
            correlation_groups.append(
                CorrelationGroup(
                    id=group_id,
                    priority=priority,
                    summary=summary,
                    services=[service],
                    signals=[tg],
                    topology=None,
                    change_candidates=changes,
                    first_seen=tg.time_window[0],
                    last_updated=tg.time_window[1],
                    event_count=tg.count,
                )
            )

        correlation_groups.sort(
            key=lambda g: (g.priority, -max(s.peak_severity for s in g.signals))
        )
        return correlation_groups

    def _compute_priority(
        self,
        peak_severity: float,
        services: list[str],
        topology: dict | None,
        topology_correlations: list,
    ) -> int:
        """Compute priority tier for a correlation group.

        P0: peak_severity > 0.8 AND service tier is "critical"
        P1: peak_severity > 0.6 OR has topology correlation with a P0 group
        P2: peak_severity > 0.3
        P3: everything else
        """
        # Check if any service is critical tier
        has_critical = False
        if topology is not None:
            for svc in services:
                svc_info = topology.get(svc, {})
                if svc_info.get("tier") == "critical":
                    has_critical = True
                    break

        if peak_severity > 0.8 and has_critical:
            return 0  # P0

        if peak_severity > 0.6:
            return 1  # P1

        # P1 if has topology correlation with a P0-eligible group
        if topology is not None and topology_correlations:
            for tc in topology_correlations:
                linked_services = {tc.primary_service}
                for rs in tc.related_services:
                    linked_services.add(rs["service"])
                if linked_services & set(services):
                    # Check if any linked service is P0-eligible
                    for svc in linked_services:
                        svc_info = topology.get(svc, {})
                        if svc_info.get("tier") == "critical":
                            return 1  # P1 — topology link to critical service

        if peak_severity > 0.3:
            return 2  # P2

        return 3  # P3

    @staticmethod
    def _dominant_event_type(signals: list[TemporalGroup]) -> str:
        """Find the most common event type across all signals."""
        type_counts: dict[str, int] = {}
        for signal in signals:
            for event in signal.events:
                t = event.type.value
                type_counts[t] = type_counts.get(t, 0) + 1

        if not type_counts:
            return "event"

        return max(type_counts, key=type_counts.get)  # type: ignore[arg-type]
