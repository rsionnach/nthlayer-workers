"""Severity pre-scoring from SLO targets. Pure arithmetic, no judgment."""
from __future__ import annotations

from nthlayer_workers.correlate.types import SitRepEvent


def pre_score(event: SitRepEvent, slo_targets: dict | None) -> float:
    """Pre-score severity using SLO targets.

    severity = min(1.0, max(0.0, (current_value - target_value) / target_value))
    Returns event.severity unchanged if no SLO context available.
    """
    if slo_targets is None or event.service not in slo_targets:
        return event.severity
    value = event.payload.get("value")
    threshold = event.payload.get("threshold")
    if value is None or threshold is None or threshold == 0:
        return event.severity
    return min(1.0, max(0.0, (value - threshold) / threshold))
