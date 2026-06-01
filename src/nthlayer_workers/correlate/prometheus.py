"""Prometheus query helpers for live correlation."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import httpx
import structlog

from nthlayer_workers.correlate.types import EventType, SitRepEvent

logger = structlog.get_logger(__name__)


async def fetch_alerts(
    client: httpx.AsyncClient,
    prometheus_url: str,
    services: set[str],
) -> list[SitRepEvent]:
    """Fetch currently firing alerts from Prometheus, filtered to given services."""
    events: list[SitRepEvent] = []
    try:
        resp = await client.get(f"{prometheus_url}/api/v1/alerts", timeout=10.0)
        resp.raise_for_status()
        alerts = resp.json().get("data", {}).get("alerts", [])
        for alert in alerts:
            if alert.get("state") != "firing":
                continue
            labels = alert.get("labels", {})
            service = labels.get("service", "")
            if service not in services:
                continue
            events.append(SitRepEvent(
                id=f"prom-alert-{uuid.uuid4().hex[:8]}",
                timestamp=alert.get("activeAt", datetime.now(timezone.utc).isoformat()),
                source="prometheus",
                type=EventType.ALERT,
                service=service,
                environment=labels.get("environment", "production"),
                severity=_alert_severity(labels.get("severity", "warning")),
                payload={
                    "alert_name": labels.get("alertname", "unknown"),
                    "labels": labels,
                    "annotations": alert.get("annotations", {}),
                },
            ))
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.debug("Failed to fetch Prometheus alerts", error=str(exc))
    return events


async def fetch_metric_breaches(
    client: httpx.AsyncClient,
    prometheus_url: str,
    services: set[str],
    window_minutes: int = 30,
) -> list[SitRepEvent]:
    """Query Prometheus for SLO metric breaches on given services."""
    events: list[SitRepEvent] = []
    now = datetime.now(timezone.utc)

    for service in services:
        # Check error budget
        budget = await _query_instant(
            client, prometheus_url,
            f'slo:error_budget:ratio{{service="{service}"}}',
        )
        if budget is not None and budget < 0.0:
            events.append(SitRepEvent(
                id=f"prom-metric-{uuid.uuid4().hex[:8]}",
                timestamp=now.isoformat(),
                source="prometheus",
                type=EventType.METRIC_BREACH,
                service=service,
                environment="production",
                severity=min(1.0, abs(budget) * 5),  # 20% deficit → severity 1.0
                payload={
                    "metric": "slo:error_budget:ratio",
                    "value": budget,
                    "breach": "error_budget_exhausted",
                },
            ))

        # Check p99 latency
        p99 = await _query_instant(
            client, prometheus_url,
            f'slo:http_request_duration_seconds:p99{{service="{service}"}}',
        )
        if p99 is not None and p99 > 0.5:  # >500ms is concerning
            events.append(SitRepEvent(
                id=f"prom-metric-{uuid.uuid4().hex[:8]}",
                timestamp=now.isoformat(),
                source="prometheus",
                type=EventType.METRIC_BREACH,
                service=service,
                environment="production",
                severity=min(1.0, p99 / 2.0),  # 2s p99 → severity 1.0
                payload={
                    "metric": "slo:http_request_duration_seconds:p99",
                    "value": p99,
                    "breach": "latency_exceeded",
                },
            ))

        # Check error rate
        error_rate = await _query_instant(
            client, prometheus_url,
            f'service:http_errors:rate5m{{service="{service}"}}',
        )
        if error_rate is not None and error_rate > 0.01:  # >1% error rate
            events.append(SitRepEvent(
                id=f"prom-metric-{uuid.uuid4().hex[:8]}",
                timestamp=now.isoformat(),
                source="prometheus",
                type=EventType.METRIC_BREACH,
                service=service,
                environment="production",
                severity=min(1.0, error_rate * 10),  # 10% error rate → severity 1.0
                payload={
                    "metric": "service:http_errors:rate5m",
                    "value": error_rate,
                    "breach": "error_rate_elevated",
                },
            ))

    return events


def verdict_to_event(verdict) -> SitRepEvent:
    """Convert a verdict from the verdict store into a SitRepEvent."""
    custom = getattr(verdict.metadata, "custom", {}) or {}
    return SitRepEvent(
        id=f"verdict-{verdict.id}",
        timestamp=verdict.timestamp.isoformat() if hasattr(verdict.timestamp, "isoformat") else str(verdict.timestamp),
        source=verdict.producer.system,
        type=EventType.VERDICT,
        service=verdict.subject.ref or verdict.subject.service or "unknown",
        environment="production",
        severity=verdict.judgment.confidence if verdict.judgment.action == "flag" else 0.2,
        payload={
            "verdict_id": verdict.id,
            "action": verdict.judgment.action,
            "confidence": verdict.judgment.confidence,
            "slo_name": custom.get("slo_name"),
            "slo_type": custom.get("slo_type"),
            "breach": custom.get("breach"),
        },
    )


def load_dependency_graph(specs_dir: str) -> dict[str, dict]:
    """Load service dependency graph from OpenSRM specs directory.

    Returns dict mapping service name → {tier, dependencies, dependents}.
    """
    from pathlib import Path

    import yaml

    graph: dict[str, dict] = {}
    specs_path = Path(specs_dir)
    if not specs_path.is_dir():
        return graph

    for spec_file in sorted(specs_path.glob("*.yaml")):
        try:
            raw = yaml.safe_load(spec_file.read_text())
        except Exception:
            continue
        if not isinstance(raw, dict):
            continue

        metadata = raw.get("metadata", {})
        service = metadata.get("name", spec_file.stem)
        spec = raw.get("spec", {})
        tier = metadata.get("tier", "standard")
        deps = [d["name"] for d in spec.get("dependencies", []) if isinstance(d, dict)]

        graph[service] = {"tier": tier, "dependencies": deps, "dependents": []}

    # Build reverse dependencies
    for svc, info in graph.items():
        for dep in info["dependencies"]:
            if dep in graph:
                graph[dep]["dependents"].append(svc)

    return graph


def blast_radius_services(
    trigger_service: str,
    dependency_graph: dict[str, dict],
) -> set[str]:
    """Compute blast radius: trigger service + dependents (upstream consumers) + dependencies (downstream)."""
    affected = {trigger_service}
    # Walk dependents (who depends on the trigger service?)
    to_visit = list(dependency_graph.get(trigger_service, {}).get("dependents", []))
    while to_visit:
        svc = to_visit.pop(0)
        if svc not in affected:
            affected.add(svc)
            to_visit.extend(dependency_graph.get(svc, {}).get("dependents", []))
    # Also include dependencies (downstream services)
    for dep in dependency_graph.get(trigger_service, {}).get("dependencies", []):
        affected.add(dep)
    return affected


async def _query_instant(
    client: httpx.AsyncClient,
    prometheus_url: str,
    query: str,
) -> float | None:
    """Execute a PromQL instant query and return the scalar value."""
    try:
        resp = await client.get(
            f"{prometheus_url}/api/v1/query",
            params={"query": query},
            timeout=10.0,
        )
        resp.raise_for_status()
        results = resp.json().get("data", {}).get("result", [])
        if not results:
            return None
        val = float(results[0].get("value", [None, None])[1])
        if val != val:  # NaN
            return None
        return val
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
        return None


def _alert_severity(severity_label: str) -> float:
    """Map Prometheus alert severity label to 0.0-1.0."""
    mapping = {"critical": 0.95, "warning": 0.6, "info": 0.3}
    return mapping.get(severity_label, 0.5)
