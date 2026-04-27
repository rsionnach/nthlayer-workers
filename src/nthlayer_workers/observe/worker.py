"""Observe worker modules — SLO collection, drift detection, topology.

Three modules implementing WorkerModule protocol, each on its own interval:
- ObserveCollectModule: SLO collection + portfolio (60s)
- ObserveDriftModule: drift analysis per service+SLO (1800s)
- ObserveTopologyModule: dependency graph / blast radius (86400s)

Deploy gate is CLI-only (not a worker module).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import structlog

from nthlayer_common.api_client import CoreAPIClient
from nthlayer_common.manifest.models import SLODefinition

from nthlayer_workers.observe.assessment import create, to_dict
from nthlayer_workers.observe.portfolio.aggregator import build_portfolio_from_results
from nthlayer_workers.observe.slo.collector import (
    SLOMetricCollector,
    SLOResult,
    ServiceSLO,
    results_to_assessments,
)

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# ObserveCollectModule — SLO collection + portfolio
# ---------------------------------------------------------------------------


@dataclass
class ObserveCollectModule:
    """SLO collection + portfolio synthesis.

    Each process_cycle():
    1. Fetches manifests from core's GET /manifests
    2. Extracts ServiceSLO pairs
    3. Queries Prometheus for SLI values via SLOMetricCollector
    4. Converts results to slo_status assessments, submits to core
    5. Builds portfolio from current-cycle results, submits portfolio_status assessment
    """

    client: CoreAPIClient
    prometheus_url: str

    def __post_init__(self):
        # Constructed once to reuse connections and avoid re-reading
        # Prometheus auth env vars on every cycle.
        self._collector = SLOMetricCollector(self.prometheus_url)

    @property
    def name(self) -> str:
        return "observe.collect"

    async def restore_state(self, state: dict | None) -> None:
        pass

    async def process_cycle(self) -> None:
        manifests = await _fetch_manifests(self.client)
        if not manifests:
            return

        service_slos = _extract_service_slos(manifests)
        if not service_slos:
            return

        by_service: dict[str, list[ServiceSLO]] = defaultdict(list)
        for item in service_slos:
            by_service[item.service].append(item)

        # Collect SLOs and track results + assessment IDs for portfolio
        all_results: dict[str, list[SLOResult]] = {}
        slo_assessment_ids: list[str] = []
        collection_errors = 0

        for service, slos in sorted(by_service.items()):
            try:
                results = await self._collector.collect(slos)
            except Exception:
                logger.exception("observe_collect_failed", service=service)
                collection_errors += 1
                continue

            all_results[service] = results
            assessments = results_to_assessments(results, service)
            for assessment in assessments:
                submit_result = await self.client.submit_assessment(to_dict(assessment))
                if submit_result.ok:
                    slo_assessment_ids.append(assessment.id)
                else:
                    logger.warning(
                        "observe_submit_failed",
                        assessment_id=assessment.id,
                        error=submit_result.error,
                    )

        # Portfolio: synthesise current-cycle results
        if all_results:
            portfolio = build_portfolio_from_results(all_results)
            portfolio_assessment = create(
                "portfolio_status",
                # "__portfolio__" sentinel: portfolio is an org-level assessment,
                # not per-service. Uses a reserved name that cannot collide with
                # real service names (OpenSRM metadata.name cannot start with _).
                "__portfolio__",
                {
                    "total_services": portfolio.total_services,
                    "healthy_count": portfolio.healthy_count,
                    "warning_count": portfolio.warning_count,
                    "critical_count": portfolio.critical_count,
                    "exhausted_count": portfolio.exhausted_count,
                    "services": [
                        {
                            "service": svc.service,
                            "overall_status": svc.overall_status,
                            "slo_count": len(svc.slos),
                        }
                        for svc in portfolio.services
                    ],
                    "collection_errors": collection_errors,
                    # parent_ids is best-effort: only includes IDs of
                    # successfully submitted SLO assessments.
                    "parent_ids": slo_assessment_ids,
                },
            )
            await self.client.submit_assessment(to_dict(portfolio_assessment))

    async def get_state(self) -> dict:
        return {}


# ---------------------------------------------------------------------------
# ObserveDriftModule — drift analysis per service+SLO
# ---------------------------------------------------------------------------


@dataclass
class ObserveDriftModule:
    """Drift detection for all SLOs across all services.

    Queries Prometheus range data directly for each service+SLO
    combination from manifests. Produces drift_signal assessments.
    """

    client: CoreAPIClient
    prometheus_url: str

    @property
    def name(self) -> str:
        return "observe.drift"

    async def restore_state(self, state: dict | None) -> None:
        pass

    async def process_cycle(self) -> None:
        from nthlayer_workers.observe.drift import DriftAnalysisError, DriftAnalyzer

        manifests = await _fetch_manifests(self.client)
        if not manifests:
            return

        analyzer = DriftAnalyzer(self.prometheus_url)

        for service, tier, slo_name, window in _drift_targets(manifests):
            try:
                result = await analyzer.analyze(service, tier, slo_name, window)
            except DriftAnalysisError:
                logger.warning("drift_analysis_failed", service=service, slo=slo_name)
                continue
            except Exception:
                logger.exception("drift_unexpected_error", service=service, slo=slo_name)
                continue

            assessment = create("drift_signal", service, {
                "slo_name": result.slo_name,
                "severity": result.severity.value,
                "pattern": result.pattern.value,
                "slope_per_week": result.metrics.slope_per_week,
                "days_until_exhaustion": result.projection.days_until_exhaustion,
                "current_budget": result.metrics.current_budget,
                "summary": result.summary,
                "recommendation": result.recommendation,
            })
            await self.client.submit_assessment(to_dict(assessment))

    async def get_state(self) -> dict:
        return {}


# ---------------------------------------------------------------------------
# ObserveTopologyModule — dependency graph / blast radius
# ---------------------------------------------------------------------------


@dataclass
class ObserveTopologyModule:
    """Dependency graph discovery and blast radius computation.

    Queries Prometheus for dependency metrics, builds graph, computes
    blast radius per service. Produces dependency_graph assessments.

    Use case for v1.5: topology observation for planning purposes.
    The "incident blast radius" use case lives in respond's correlation,
    not here. Daily cadence is appropriate.

    Scale note: per-service loop over calculate_blast_radius() builds a
    single graph then evaluates each service. Fine for v1.5; may need a
    bulk API if service count grows meaningfully (100+).
    """

    client: CoreAPIClient
    prometheus_url: str

    @property
    def name(self) -> str:
        return "observe.topology"

    async def restore_state(self, state: dict | None) -> None:
        pass

    async def process_cycle(self) -> None:
        from nthlayer_workers.observe.dependencies import DependencyDiscovery
        from nthlayer_workers.observe.dependencies.providers.prometheus import PrometheusDepProvider

        manifests = await _fetch_manifests(self.client)
        if not manifests:
            return

        discovery = DependencyDiscovery()
        discovery.add_provider(PrometheusDepProvider(url=self.prometheus_url))

        service_names = [m["name"] for m in manifests if m.get("name")]
        if not service_names:
            return

        try:
            graph = await discovery.build_graph(service_names)
        except Exception:
            logger.exception("topology_graph_build_failed")
            return

        for m in manifests:
            service = m.get("name")
            if not service:
                continue
            try:
                result = discovery.calculate_blast_radius(service, graph)
            except Exception:
                logger.exception("topology_blast_radius_failed", service=service)
                continue

            assessment = create("dependency_graph", service, {
                "total_services_affected": result.total_services_affected,
                "critical_services_affected": result.critical_services_affected,
                "risk_level": result.risk_level,
                "direct_downstream_count": len(result.direct_downstream),
                "recommendation": result.recommendation,
            })
            await self.client.submit_assessment(to_dict(assessment))

    async def get_state(self) -> dict:
        return {}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _fetch_manifests(client: CoreAPIClient) -> list[dict] | None:
    """Fetch manifests from core API. Returns None on error, [] on empty catalogue."""
    result = await client.get_manifests()
    if not result.ok:
        logger.warning("observe_manifest_fetch_failed", error=result.error)
        return None
    return result.data or []


def _extract_service_slos(manifest_dicts: list[dict]) -> list[ServiceSLO]:
    """Extract ServiceSLO pairs from core API manifest response dicts.

    Parallel to spec_loader.load_specs(): that path reads YAML files on disk
    (CLI use), this path parses manifest dicts from core's GET /manifests
    (worker use).
    """
    results: list[ServiceSLO] = []
    for m in manifest_dicts:
        service = m.get("name")
        if not service:
            continue
        for slo_dict in m.get("slos", []):
            name = slo_dict.get("name")
            target = slo_dict.get("target")
            if not name or target is None:
                continue
            slo = SLODefinition(
                name=name,
                target=target,
                slo_type=slo_dict.get("slo_type", "availability"),
                window=slo_dict.get("window", "30d"),
                indicator_query=slo_dict.get("indicator_query"),
                total_query=slo_dict.get("total_query"),
                good_query=slo_dict.get("good_query"),
                judgment_type=slo_dict.get("judgment_type"),
            )
            results.append(ServiceSLO(service=service, slo=slo))
    return results


def _drift_targets(manifest_dicts: list[dict]) -> list[tuple[str, str, str, str | None]]:
    """Extract (service, tier, slo_name, window) tuples for drift analysis."""
    targets: list[tuple[str, str, str, str | None]] = []
    for m in manifest_dicts:
        service = m.get("name")
        tier = m.get("tier", "standard")
        if not service:
            continue
        for slo_dict in m.get("slos", []):
            slo_name = slo_dict.get("name")
            if not slo_name:
                continue
            window = slo_dict.get("window")
            targets.append((service, tier, slo_name, window))
    return targets
