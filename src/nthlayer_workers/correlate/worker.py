"""Correlate worker modules — session windows, topology drift, contract divergence.

Three modules implementing WorkerModule protocol:
- CorrelateSessionModule: event correlation via session windows (10s)
- CorrelateTopologyModule: declared vs observed topology from Tempo (1h)
- CorrelateContractModule: promised vs observed performance from Prometheus (1h)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from nthlayer_common.api_client import CoreAPIClient
from nthlayer_common.cloudevents import wrap_assessment

from nthlayer_workers.correlate.session import (
    CorrelationDomain,
    SessionWindow,
    SessionWindowManager,
)
from nthlayer_workers.correlate.types import EventType, SitRepEvent

logger = structlog.get_logger()


@dataclass
class CorrelateSessionModule:
    """Session window-based event correlation.

    Each process_cycle():
    1. Polls core API for new verdicts + assessments since cursor
    2. Converts to SitRepEvents, ingests into session windows
    3. Closes windows that hit their close conditions
    4. For each closed window, builds correlation_snapshot assessment
    5. Submits snapshots to core, persists cursor + window state
    """

    client: CoreAPIClient
    gap_seconds: float = 60.0
    max_duration_seconds: float = 900.0

    def __post_init__(self):
        self._manager = SessionWindowManager(
            gap_seconds=self.gap_seconds,
            max_duration_seconds=self.max_duration_seconds,
        )
        self._cursor: str | None = None

    @property
    def name(self) -> str:
        return "correlate"

    async def restore_state(self, state: dict | None) -> None:
        """Restore cursor and open window metadata from component_state."""
        if not state:
            return
        self._cursor = state.get("cursor")
        windows = state.get("windows", {})
        for key, metadata in windows.items():
            parts = key.split(":", 1)
            if len(parts) == 2:
                domain = CorrelationDomain(service=parts[0], environment=parts[1])
                self._manager.restore_window(domain, metadata)
                logger.info("correlate_window_restored", domain=str(domain))

    async def process_cycle(self) -> None:
        """One correlation cycle."""
        # 1. Poll core for new events
        events = await self._poll_events()
        if not events:
            # Still check for windows to close (gap timeout)
            closed = self._manager.close_ready()
            for window in closed:
                await self._emit_snapshot(window)
            return

        # 2. Ingest into session windows
        for event in events:
            self._manager.ingest(event)

        # 3. Close ready windows
        closed = self._manager.close_ready()

        # 4. Emit snapshots for closed windows
        for window in closed:
            await self._emit_snapshot(window)

    async def get_state(self) -> dict:
        """Return cursor + open window metadata for persistence."""
        state: dict[str, Any] = {}
        if self._cursor:
            state["cursor"] = self._cursor
        state["windows"] = self._manager.to_state()
        return state

    async def _poll_events(self) -> list[SitRepEvent]:
        """Poll core API for new verdicts and assessments since cursor."""
        events: list[SitRepEvent] = []

        # Poll verdicts (quality_breach, autonomy_change)
        verdict_params: dict[str, Any] = {"limit": 100}
        if self._cursor:
            verdict_params["created_after"] = self._cursor
        for vtype in ("quality_breach", "autonomy_change"):
            result = await self.client.get_verdicts(verdict_type=vtype, **verdict_params)
            if result.ok and result.data:
                for v in result.data:
                    events.append(verdict_to_event(v))

        # Poll assessments (slo_status, drift_signal).
        # v1.5 limitation: assessments API has no created_after filter.
        # Client-side filtering against cursor. If volume exceeds limit=100,
        # newer assessments beyond page boundary are silently missed until
        # the next cycle. Tracked as a known v1.5 constraint.
        cursor_dt = _parse_iso(self._cursor) if self._cursor else None
        for kind in ("slo_status", "drift_signal"):
            result = await self.client.get_assessments(kind=kind, limit=100)
            if result.ok and result.data:
                for a in result.data:
                    if cursor_dt:
                        a_dt = _parse_iso(a.get("created_at", ""))
                        if a_dt and a_dt <= cursor_dt:
                            continue
                    events.append(assessment_to_event(a))

        # Update cursor to most recent event
        if events:
            latest = max(
                (e.timestamp for e in events),
                default=self._cursor,
            )
            if isinstance(latest, str):
                self._cursor = latest
            elif isinstance(latest, datetime):
                self._cursor = latest.isoformat()

        return events

    async def _emit_snapshot(self, window: SessionWindow) -> None:
        """Build and submit a correlation_snapshot assessment for a closed window."""
        if not window.events:
            # Orphaned window from crash recovery with no re-fetched events.
            # Skip rather than producing a phantom empty snapshot.
            return
        now = datetime.now(timezone.utc)
        reason = window.close_reason(now, self.gap_seconds, self.max_duration_seconds)

        # Verdict IDs of QUALITY_SCORE events that triggered this snapshot.
        # respond's open_from_snapshot reads parent_ids to anchor cases on a
        # verdict id (snapshots are assessments; assessment ids would 404 on
        # the bench's verdict-ancestors lineage walk).
        # See docs/superpowers/decisions/verdict-assessment-taxonomy-boundary.md
        # See docs/superpowers/decisions/eager-case-creation.md
        trigger_verdict_ids = [
            e.id for e in window.events if e.type == EventType.QUALITY_SCORE
        ]

        snapshot_data = {
            "domain": {
                "service": window.domain.service,
                "environment": window.domain.environment,
            },
            "window": {
                "opened_at": window.opened_at.isoformat(),
                "closed_at": now.isoformat(),
                "duration_seconds": (now - window.opened_at).total_seconds(),
                "close_reason": reason,
            },
            "event_count": len(window.events),
            "event_types": _count_by_type(window.events),
            "peak_severity": max((e.severity for e in window.events), default=0.0),
            "affected_services": sorted({e.service for e in window.events}),
            # "default" for v1.5 — assessments don't carry environment.
            # v2 adds "manifest" and "event" sources.
            "environment_source": "default",
            # parent_ids: triggering breach verdicts. Read by
            # respond.worker_helpers.open_from_snapshot for incident
            # context lineage and by respond's case-creation path for
            # the case's underlying_verdict anchor.
            "parent_ids": trigger_verdict_ids,
        }

        # P3-D.3: NL summary — non-blocking, 5s timeout
        from nthlayer_workers.correlate.summary import generate_summary
        snapshot_data["nl_summary"] = await generate_summary(snapshot_data, window.events)

        assessment = {
            "id": f"csn-{window.domain.service}-{window.domain.environment}-{uuid.uuid4().hex[:8]}",
            "created_at": now.isoformat(),
            "kind": "correlation_snapshot",
            "service": window.domain.service,
            "data": snapshot_data,
        }

        result = await self.client.submit_assessment(wrap_assessment(assessment, component="correlate"))
        if not result.ok:
            logger.warning(
                "correlate_snapshot_submit_failed",
                domain=str(window.domain),
                error=result.error,
            )
        else:
            logger.info(
                "correlate_snapshot_emitted",
                domain=str(window.domain),
                event_count=len(window.events),
                close_reason=reason,
            )


# ---------------------------------------------------------------------------
# Event conversion
# ---------------------------------------------------------------------------


def verdict_to_event(verdict: dict) -> SitRepEvent:
    """Convert a core verdict dict to a SitRepEvent."""
    return SitRepEvent(
        id=verdict.get("id", "unknown"),
        timestamp=verdict.get("created_at", datetime.now(timezone.utc).isoformat()),
        source=f"verdict:{verdict.get('type', 'unknown')}",
        type=EventType.QUALITY_SCORE if verdict.get("type") == "quality_breach" else EventType.VERDICT,
        service=verdict.get("service", "unknown"),
        environment=verdict.get("environment", "production"),
        severity=_severity_from_verdict(verdict),
        payload=verdict,
    )


def assessment_to_event(assessment: dict) -> SitRepEvent:
    """Convert a core assessment dict to a SitRepEvent."""
    return SitRepEvent(
        id=assessment.get("id", "unknown"),
        timestamp=assessment.get("created_at", datetime.now(timezone.utc).isoformat()),
        source=f"assessment:{assessment.get('kind', 'unknown')}",
        type=EventType.METRIC_BREACH if _is_breach(assessment) else EventType.ALERT,
        service=assessment.get("service", "unknown"),
        # Assessments don't carry environment; default production.
        # Known limitation — v2 adds environment to assessment model.
        environment="production",
        severity=_severity_from_assessment(assessment),
        payload=assessment,
    )


def _severity_from_verdict(verdict: dict) -> float:
    """Extract severity 0.0-1.0 from a verdict."""
    # quality_breach is always high severity
    if verdict.get("type") == "quality_breach":
        return 0.9
    return 0.5


def _severity_from_assessment(assessment: dict) -> float:
    """Extract severity 0.0-1.0 from an assessment."""
    status = assessment.get("data", {}).get("status", "")
    return {
        "EXHAUSTED": 1.0,
        "CRITICAL": 0.8,
        "WARNING": 0.5,
        "HEALTHY": 0.1,
    }.get(status, 0.3)


def _is_breach(assessment: dict) -> bool:
    """Check if an assessment represents a breach condition."""
    status = assessment.get("data", {}).get("status", "")
    return status in ("EXHAUSTED", "CRITICAL")


def _count_by_type(events: list[SitRepEvent]) -> dict[str, int]:
    """Count events by type."""
    counts: dict[str, int] = {}
    for e in events:
        counts[e.type.value] = counts.get(e.type.value, 0) + 1
    return counts


def _parse_iso(ts: str | None) -> datetime | None:
    """Parse ISO 8601 timestamp, handling both Z and +00:00 suffixes."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# CorrelateTopologyModule — declared vs observed topology from Tempo
# ---------------------------------------------------------------------------


@dataclass
class CorrelateTopologyModule:
    """Periodic topology drift detection.

    Compares declared dependencies (manifests) against observed
    service-to-service edges (Tempo traces). Produces topology_drift
    assessments when mismatches are found.

    No-op when trace_backend is None (Tempo not configured).
    """

    client: CoreAPIClient
    trace_backend: Any = None  # TempoTraceBackend or None
    _logged_no_backend: bool = False

    @property
    def _configured(self) -> bool:
        return self.trace_backend is not None

    @property
    def name(self) -> str:
        return "correlate.topology"

    async def restore_state(self, state: dict | None) -> None:
        pass

    async def process_cycle(self) -> None:
        if not self._configured:
            if not self._logged_no_backend:
                logger.info("topology_module_no_trace_backend")
                self._logged_no_backend = True
            return

        from nthlayer_workers.correlate.traces.topology import detect_topology_divergence

        # Fetch manifests
        result = await self.client.get_manifests()
        if not result.ok or not result.data:
            return

        manifests = result.data

        # Build declared deps from manifests
        declared_deps: dict[str, dict] = {}
        for m in manifests:
            service = m.get("name")
            if not service:
                continue
            deps = [d.get("name") for d in m.get("dependencies", []) if d.get("name")]
            declared_deps[service] = {"dependencies": deps}

        # Get trace evidence for all services
        service_names = list(declared_deps.keys())
        try:
            end = datetime.now(timezone.utc)
            start = end - timedelta(hours=1)
            evidence = await self.trace_backend.get_trace_evidence(
                services=service_names,
                start=start,
                end=end,
            )
        except Exception:
            logger.warning("topology_trace_query_failed")
            return

        if not evidence or not evidence.services:
            return

        # Detect topology divergence
        divergence = detect_topology_divergence(evidence, declared_deps)

        # Check guarantee mismatches from edge data
        guarantee_mismatches = _check_guarantee_mismatches(manifests, evidence)

        has_drift = (
            divergence.declared_not_observed
            or divergence.observed_not_declared
            or guarantee_mismatches
        )

        if not has_drift:
            return

        # Emit one assessment per divergence (org-level)
        now = datetime.now(timezone.utc)
        assessment = {
            "id": f"tdr-topology-{uuid.uuid4().hex[:8]}",
            "created_at": now.isoformat(),
            "kind": "topology_drift",
            "service": "__topology__",
            "data": {
                "declared_not_observed": [
                    {"source": s, "target": t}
                    for s, t in divergence.declared_not_observed
                ],
                "observed_not_declared": [
                    {"source": s, "target": t}
                    for s, t in divergence.observed_not_declared
                ],
                "guarantee_mismatches": guarantee_mismatches,
                "total_declared": sum(
                    len(v.get("dependencies", []))
                    for v in declared_deps.values()
                ),
                "total_observed": len({
                    (e.source_service, e.target_service)
                    for svc in evidence.services
                    for e in svc.callees
                }),
                "drift_detected": True,
            },
        }
        await self.client.submit_assessment(wrap_assessment(assessment, component="correlate"))

    async def get_state(self) -> dict:
        return {}


def _check_guarantee_mismatches(
    manifests: list[dict], evidence: Any
) -> list[dict]:
    """Check if observed edge metrics violate declared dependency guarantees."""
    # Build edge lookup from trace evidence
    edge_lookup: dict[tuple[str, str], Any] = {}
    for svc_profile in evidence.services:
        for edge in svc_profile.callees:
            edge_lookup[(edge.source_service, edge.target_service)] = edge

    mismatches: list[dict] = []
    for m in manifests:
        service = m.get("name")
        if not service:
            continue
        for dep in m.get("dependencies", []):
            dep_name = dep.get("name")
            if not dep_name:
                continue
            edge = edge_lookup.get((service, dep_name))
            if not edge:
                continue

            # Check availability (error rate)
            slo = dep.get("slo", {})
            if slo and isinstance(slo, dict):
                promised_avail = slo.get("availability")
                if promised_avail is not None and edge.request_count > 0:
                    observed_avail = 1.0 - (edge.error_count / edge.request_count)
                    if observed_avail < promised_avail:
                        mismatches.append({
                            "source": service,
                            "target": dep_name,
                            "metric": "availability",
                            "promised": promised_avail,
                            "observed": round(observed_avail, 6),
                            "tempo_window": "1h",
                        })

                promised_latency = slo.get("latency_p99")
                if promised_latency is not None and edge.request_count > 0:
                    # Parse "200ms" → 200.0
                    promised_ms = _parse_latency_ms(promised_latency)
                    if promised_ms is not None and edge.p99_latency_ms > promised_ms:
                        mismatches.append({
                            "source": service,
                            "target": dep_name,
                            "metric": "latency_p99",
                            "promised": promised_latency,
                            "observed": f"{edge.p99_latency_ms:.0f}ms",
                            "tempo_window": "1h",
                        })

    return mismatches


def _parse_latency_ms(latency_str: str) -> float | None:
    """Parse latency string like '200ms' or '1s' to milliseconds."""
    s = latency_str.strip().lower()
    if s.endswith("ms"):
        try:
            return float(s[:-2])
        except ValueError:
            return None
    if s.endswith("s"):
        try:
            return float(s[:-1]) * 1000
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# CorrelateContractModule — promised vs observed from Prometheus
# ---------------------------------------------------------------------------


@dataclass
class CorrelateContractModule:
    """Periodic contract divergence detection.

    Compares reliability contract promises (manifests) against observed
    performance (Prometheus). Produces contract_divergence assessments
    when promises are violated.
    """

    client: CoreAPIClient
    prometheus_url: str

    @property
    def name(self) -> str:
        return "correlate.contract"

    async def restore_state(self, state: dict | None) -> None:
        pass

    async def process_cycle(self) -> None:
        from nthlayer_common.providers import PrometheusProvider

        result = await self.client.get_manifests()
        if not result.ok or not result.data:
            return

        manifests = result.data
        provider = PrometheusProvider(self.prometheus_url)

        try:
            for m in manifests:
                service = m.get("name")
                contracts = m.get("contracts", [])
                slos = m.get("slos", [])
                if not service or not contracts:
                    continue

                try:
                    await self._check_service_contracts(
                        service, contracts, slos, provider,
                    )
                except Exception:
                    logger.warning("contract_check_failed", service=service)
        finally:
            await provider.aclose()

    async def _check_service_contracts(
        self,
        service: str,
        contracts: list[dict],
        slos: list[dict],
        provider: Any,
    ) -> None:
        """Check all contracts for a single service."""
        for contract in contracts:
            promise = contract.get("promise", {})
            divergences: list[dict] = []

            # Check availability promise (must be ratio 0.0-1.0)
            promised_avail = promise.get("availability")
            if promised_avail is not None:
                if promised_avail > 1.0:
                    logger.warning("contract_availability_not_ratio",
                                   service=service, value=promised_avail)
                    continue
                observed = await self._query_availability(service, slos, provider)
                if observed is not None and observed < promised_avail:
                    divergences.append({
                        "metric": "availability",
                        "promised": promised_avail,
                        "observed": round(observed, 6),
                        "window": "30d",
                    })

            # Check latency promise
            promised_latency = promise.get("latency_p99")
            if promised_latency:
                observed_ms = await self._query_latency(service, slos, provider)
                promised_ms = _parse_latency_ms(promised_latency)
                if observed_ms is not None and promised_ms is not None and observed_ms > promised_ms:
                    divergences.append({
                        "metric": "latency_p99",
                        "promised": promised_latency,
                        "observed": f"{observed_ms:.0f}ms",
                        "window": "30d",
                    })

            if not divergences:
                continue

            now = datetime.now(timezone.utc)
            assessment = {
                "id": f"cdv-{service}-{uuid.uuid4().hex[:8]}",
                "created_at": now.isoformat(),
                "kind": "contract_divergence",
                "service": service,
                "data": {
                    "contract_name": contract.get("name", f"{service}-contract"),
                    "divergences": divergences,
                    "total_promises": sum(1 for k in ("availability", "latency_p99") if promise.get(k)),
                    "divergence_count": len(divergences),
                },
            }
            result = await self.client.submit_assessment(wrap_assessment(assessment, component="correlate"))
            if not result.ok:
                logger.warning(
                    "contract_divergence_submit_failed",
                    service=service,
                    error=result.error,
                )

    async def _query_availability(
        self, service: str, slos: list[dict], provider: Any
    ) -> float | None:
        """Query Prometheus for observed availability over 30d."""
        avail_slo = next(
            (s for s in slos if s.get("name") == "availability" and s.get("indicator_query")),
            None,
        )
        if not avail_slo:
            return None
        query = avail_slo["indicator_query"]
        try:
            return await provider.get_sli_value(query)
        except Exception:
            logger.debug("contract_availability_query_failed", service=service)
            return None

    async def _query_latency(
        self, service: str, slos: list[dict], provider: Any
    ) -> float | None:
        """Query Prometheus for observed p99 latency in ms over 30d."""
        latency_slo = next(
            (s for s in slos if s.get("slo_type") == "latency" and s.get("indicator_query")),
            None,
        )
        if not latency_slo:
            return None
        query = latency_slo["indicator_query"]
        try:
            value = await provider.get_sli_value(query)
            if value is None:
                return None
            # Prometheus histogram_quantile returns seconds (standard
            # _seconds metric convention). Convert to ms for comparison
            # with ContractPromise.latency_p99 which uses "200ms" format.
            return value * 1000
        except Exception:
            logger.debug("contract_latency_query_failed", service=service)
            return None

    async def get_state(self) -> dict:
        return {}
