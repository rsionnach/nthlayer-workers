"""Deployment correlation engine — 5-factor weighted scoring.

Deterministic heuristic that correlates deployments with error budget burns.
Adapted from nthlayer.slos.correlator to work with pre-computed inputs
instead of SLORepository queries.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Confidence thresholds
HIGH_CONFIDENCE = 0.7
MEDIUM_CONFIDENCE = 0.5
LOW_CONFIDENCE = 0.3
BLOCKING_CONFIDENCE = 0.8

# Factor weights
WEIGHTS = {
    "burn_rate": 0.35,
    "proximity": 0.25,
    "magnitude": 0.15,
    "dependency": 0.15,
    "history": 0.10,
}


@dataclass
class CorrelationInput:
    """Pre-computed input data for correlation scoring.

    Callers gather this data from assessments or other sources
    before calling the correlator.
    """

    deployment_id: str
    service: str
    deploy_time: datetime
    burn_detected_at: datetime  # when burn was first detected (for proximity scoring)
    burn_rate_before: float  # burn rate per minute before deploy window
    burn_rate_after: float   # burn rate per minute after deploy window
    burn_minutes: float      # total burn in after window
    is_same_service: bool = False   # deployment targets same service as affected SLO
    is_direct_upstream: bool = False  # deployment targets direct upstream dependency
    is_transitive_upstream: bool = False  # deployment targets transitive upstream
    is_yaml_downstream: bool = False  # service listed in YAML downstream_services
    recent_deploy_count: int = 0   # total deploys for service in history window
    prior_correlations: int = 0    # prior medium+ correlations in history window


@dataclass
class CorrelationResult:
    """Result of correlation analysis."""

    deployment_id: str
    service: str
    burn_minutes: float
    confidence: float
    method: str = "time_window_analysis"
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def confidence_label(self) -> str:
        if self.confidence >= HIGH_CONFIDENCE:
            return "HIGH"
        elif self.confidence >= MEDIUM_CONFIDENCE:
            return "MEDIUM"
        elif self.confidence >= LOW_CONFIDENCE:
            return "LOW"
        return "NONE"


def correlate(inp: CorrelationInput) -> CorrelationResult:
    """Correlate a deployment with error budget burns using 5-factor scoring.

    All data is pre-computed in CorrelationInput — no async, no DB queries.

    Factors:
    - burn_rate (0.35): Spike in burn rate after deploy
    - proximity (0.25): Time proximity (exponential decay, half-life ~30min)
    - magnitude (0.15): Absolute burn amount
    - dependency (0.15): Relationship between deploying and affected service
    - history (0.10): Historical correlation pattern
    """
    burn_rate_score = _calculate_burn_rate_score(inp.burn_rate_before, inp.burn_rate_after)
    proximity_score = _calculate_proximity_score(inp.deploy_time, inp.burn_detected_at)
    magnitude_score = _calculate_magnitude_score(inp.burn_minutes)
    dependency_score = _calculate_dependency_score(inp)
    history_score = _calculate_history_score(inp.recent_deploy_count, inp.prior_correlations)

    confidence = (
        WEIGHTS["burn_rate"] * burn_rate_score
        + WEIGHTS["proximity"] * proximity_score
        + WEIGHTS["magnitude"] * magnitude_score
        + WEIGHTS["dependency"] * dependency_score
        + WEIGHTS["history"] * history_score
    )

    return CorrelationResult(
        deployment_id=inp.deployment_id,
        service=inp.service,
        burn_minutes=inp.burn_minutes,
        confidence=confidence,
        details={
            "burn_rate_before": inp.burn_rate_before,
            "burn_rate_after": inp.burn_rate_after,
            "burn_rate_score": burn_rate_score,
            "proximity_score": proximity_score,
            "magnitude_score": magnitude_score,
            "dependency_score": dependency_score,
            "history_score": history_score,
        },
    )


def _calculate_burn_rate_score(before_rate: float, after_rate: float) -> float:
    """Spike ratio: 5x or more = 1.0. No baseline → absolute rate."""
    if before_rate == 0:
        return min(after_rate / 0.1, 1.0)
    return min((after_rate / before_rate) / 5.0, 1.0)


def _calculate_proximity_score(deployed_at: datetime, burn_detected_at: datetime) -> float:
    """Exponential decay: half-life ~30 minutes."""
    elapsed = abs((burn_detected_at - deployed_at).total_seconds()) / 60.0
    return math.exp(-elapsed / 30.0)


def _calculate_magnitude_score(burn_minutes: float) -> float:
    """Absolute burn: 10+ minutes = 1.0."""
    return min(burn_minutes / 10.0, 1.0)


def _calculate_dependency_score(inp: CorrelationInput) -> float:
    """Relationship score: same=1.0, direct upstream=1.0, transitive=0.4, yaml=0.6, none=0.0."""
    if inp.is_same_service:
        return 1.0
    if inp.is_direct_upstream:
        return 1.0
    if inp.is_transitive_upstream:
        return 0.4
    if inp.is_yaml_downstream:
        return 0.6
    return 0.0


def _calculate_history_score(recent_deploy_count: int, prior_correlations: int) -> float:
    """Repeat offender penalty: fraction of recent deploys with medium+ correlation."""
    if recent_deploy_count == 0:
        return 0.0
    return min(prior_correlations / recent_deploy_count, 1.0)
