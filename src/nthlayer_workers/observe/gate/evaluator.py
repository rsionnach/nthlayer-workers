"""Deployment gate evaluation — decides APPROVED/WARNING/BLOCKED from assessments.

Reads slo_status assessments to determine error budget status,
then applies tier-based thresholds to make gate decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nthlayer_common.gate_models import GatePolicy, GateResult
from nthlayer_common.tiers import TIER_CONFIGS

from nthlayer_workers.observe.store import AssessmentFilter, AssessmentStore

# Default thresholds from tier config
THRESHOLDS: dict[str, dict[str, float | None]] = {
    tier: {
        "warning": config.error_budget_warning_pct,
        "blocking": config.error_budget_blocking_pct,
    }
    for tier, config in TIER_CONFIGS.items()
}


@dataclass
class GateCheckResult:
    """Result of a deployment gate check."""

    service: str
    tier: str
    result: GateResult
    budget_remaining_pct: float
    warning_threshold: float
    blocking_threshold: float | None
    message: str
    recommendations: list[str]
    slo_count: int = 0


def check_deploy(
    service: str,
    tier: str,
    store: AssessmentStore,
    policy: GatePolicy | None = None,
) -> GateCheckResult:
    """Check if deployment should be allowed based on assessment data.

    Reads recent slo_status assessments for the service, computes
    aggregate budget consumption, and applies tier thresholds.

    Args:
        service: Service name
        tier: Service tier (critical, standard, low)
        store: Assessment store with slo_status assessments
        policy: Optional custom GatePolicy to override tier defaults

    Returns:
        GateCheckResult with APPROVED/WARNING/BLOCKED decision
    """
    # Get recent slo_status assessments for this service
    assessments = store.query(
        AssessmentFilter(service=service, kind="slo_status")
    )

    if not assessments:
        return GateCheckResult(
            service=service,
            tier=tier,
            result=GateResult.APPROVED,
            budget_remaining_pct=100.0,
            warning_threshold=_get_warning_threshold(tier, policy),
            blocking_threshold=_get_blocking_threshold(tier, policy),
            message="No SLO assessments found — gate approved by default",
            recommendations=[],
            slo_count=0,
        )

    # Compute aggregate budget consumption from latest assessments per SLO
    latest_by_slo: dict[str, dict[str, Any]] = {}
    for a in assessments:
        slo_name = a.data.get("slo_name", "unknown")
        if slo_name not in latest_by_slo:
            latest_by_slo[slo_name] = a.data

    consumed_values = [
        d.get("percent_consumed", 0.0)
        for d in latest_by_slo.values()
        if d.get("percent_consumed") is not None
    ]

    if not consumed_values:
        avg_consumed = 0.0
    else:
        avg_consumed = sum(consumed_values) / len(consumed_values)

    budget_remaining_pct = max(0.0, 100.0 - avg_consumed)

    # Get thresholds
    warning_threshold = _get_warning_threshold(tier, policy)
    blocking_threshold = _get_blocking_threshold(tier, policy)

    # Evaluate gate
    result, message, recommendations = _evaluate_thresholds(
        budget_remaining_pct, warning_threshold, blocking_threshold, tier, policy
    )

    return GateCheckResult(
        service=service,
        tier=tier,
        result=result,
        budget_remaining_pct=budget_remaining_pct,
        warning_threshold=warning_threshold,
        blocking_threshold=blocking_threshold,
        message=message,
        recommendations=recommendations,
        slo_count=len(latest_by_slo),
    )


def _get_warning_threshold(tier: str, policy: GatePolicy | None) -> float:
    """Get warning threshold from policy or tier defaults."""
    if policy and policy.warning is not None:
        return policy.warning
    defaults = THRESHOLDS.get(tier, THRESHOLDS["standard"])
    return defaults.get("warning") or 20.0


def _get_blocking_threshold(tier: str, policy: GatePolicy | None) -> float | None:
    """Get blocking threshold from policy or tier defaults."""
    if policy and policy.blocking is not None:
        return policy.blocking
    defaults = THRESHOLDS.get(tier, THRESHOLDS["standard"])
    return defaults.get("blocking")


def _evaluate_thresholds(
    budget_remaining_pct: float,
    warning_threshold: float,
    blocking_threshold: float | None,
    tier: str,
    policy: GatePolicy | None,
) -> tuple[GateResult, str, list[str]]:
    """Apply threshold logic to determine gate result."""
    recommendations: list[str] = []

    # Check exhaustion
    if budget_remaining_pct <= 0:
        if policy and policy.on_exhausted:
            if "freeze_deploys" in policy.on_exhausted:
                return (
                    GateResult.BLOCKED,
                    "Error budget exhausted (0% remaining). Deployment frozen per policy.",
                    ["Wait for error budget to recover before deploying"],
                )
            if "require_approval" in policy.on_exhausted:
                return (
                    GateResult.WARNING,
                    "Error budget exhausted. Manual approval required per policy.",
                    ["Get explicit approval before proceeding"],
                )

    # Check blocking threshold
    if blocking_threshold is not None and budget_remaining_pct <= blocking_threshold:
        return (
            GateResult.BLOCKED,
            f"Error budget critical: {budget_remaining_pct:.1f}% remaining (blocking threshold: {blocking_threshold}%)",
            [
                f"Budget below blocking threshold ({blocking_threshold}%)",
                "Investigate ongoing issues before deploying",
                "Consider waiting for budget recovery",
            ],
        )

    # Check warning threshold
    if budget_remaining_pct <= warning_threshold:
        recommendations.append(f"Budget below warning threshold ({warning_threshold}%)")
        recommendations.append("Monitor closely after deployment")
        return (
            GateResult.WARNING,
            f"Error budget low: {budget_remaining_pct:.1f}% remaining (warning threshold: {warning_threshold}%)",
            recommendations,
        )

    # Approved
    return (
        GateResult.APPROVED,
        f"Error budget healthy: {budget_remaining_pct:.1f}% remaining",
        [],
    )
