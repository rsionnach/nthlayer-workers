"""Shared data types for the nthlayer-measure pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class AutonomyLevel(StrEnum):
    """Agent autonomy levels managed by governance.

    Five named levels ordered from most to least autonomous.
    One-way ratchet down — automatic reduction only, manual elevation only.
    """

    FULLY_AUTONOMOUS = "fully_autonomous"
    AUTONOMOUS = "autonomous"
    LIMITED_AUTONOMOUS = "limited_autonomous"
    ADVISOR = "advisor"
    OBSERVER = "observer"


@dataclass(frozen=True)
class AgentOutput:
    """Normalized output from any adapter — the universal input to evaluation."""

    agent_name: str
    task_id: str
    output_content: str
    output_type: str
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class QualityScore:
    """Complete evaluation result for a single agent output."""

    eval_id: str
    agent_name: str
    task_id: str
    dimensions: dict[str, float]
    reasoning: dict[str, str] = field(default_factory=dict)
    confidence: float = 0.0
    evaluator_model: str = ""
    cost_usd: float | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    tier: str | None = None
    auto_approved: bool = False


@dataclass(frozen=True)
class GovernanceAction:
    """Action taken by the governance engine."""

    agent_name: str
    target_level: AutonomyLevel
    reason: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class TrendWindow:
    """Aggregated trend data over a time window."""

    agent_name: str
    window_days: int
    dimension_averages: dict[str, float]
    evaluation_count: int
    confidence_mean: float
    reversal_rate: float = 0.0
    total_cost_usd: float = 0.0
    avg_cost_per_eval: float = 0.0
