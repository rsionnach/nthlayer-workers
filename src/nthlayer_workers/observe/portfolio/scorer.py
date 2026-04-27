"""Simple reliability scoring from portfolio data."""

from __future__ import annotations

from nthlayer_workers.observe.portfolio.aggregator import ServiceHealth


def score_service(health: ServiceHealth) -> float:
    """Score a service based on its SLO compliance (0-100)."""
    if not health.slos:
        return 0.0
    healthy = sum(1 for s in health.slos if s.status == "HEALTHY")
    return (healthy / len(health.slos)) * 100
