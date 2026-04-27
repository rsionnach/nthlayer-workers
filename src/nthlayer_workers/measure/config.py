"""Configuration loading for nthlayer-measure."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class AgentConfig:
    """Configuration for a monitored agent."""

    name: str
    adapter: str = "webhook"
    dimensions: list[str] | None = None
    manifest: str | None = None
    adapter_config: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvaluatorConfig:
    """Configuration for the evaluation model."""

    model: str = os.environ.get("NTHLAYER_MODEL", "claude-sonnet-4-20250514")
    max_tokens: int = 4096
    temperature: float = 0.0


@dataclass
class StoreConfig:
    """Configuration for the score store."""

    backend: str = "sqlite"
    path: str = "measure.db"


@dataclass
class GovernanceConfig:
    """Configuration for the governance engine."""

    error_budget_window_days: int = 7
    error_budget_threshold: float = 0.5


@dataclass
class DetectionConfig:
    """Configuration for degradation detection thresholds."""

    max_reversal_rate: float = 0.3
    min_dimension_scores: dict[str, float] = field(default_factory=dict)
    min_confidence: float = 0.5


@dataclass
class VerdictConfig:
    """Configuration for verdict integration."""

    store_path: str = "verdicts.db"


@dataclass
class TriggerConfig:
    """Configuration for downstream trigger chain."""

    correlate_enabled: bool = False
    correlate_args: dict[str, str] = field(default_factory=dict)
    respond_enabled: bool = False
    respond_args: dict[str, str] = field(default_factory=dict)


@dataclass
class TieringConfig:
    """Configuration for tiered evaluation."""

    enabled: bool = False
    default_tier: str = "standard"
    auto_approve_score: float = 1.0
    models: dict[str, str] = field(default_factory=lambda: {
        "standard": "anthropic/claude-haiku-4-20250414",
        "deep": "anthropic/claude-sonnet-4-20250514",
        "critical": "anthropic/claude-opus-4-20250514",
    })
    sampling_rate: float = 0.05
    sampling_window_size: int = 100
    quality_threshold: float = 0.6
    promotion_threshold: float = 0.10


@dataclass
class MeasureConfig:
    """Top-level nthlayer-measure configuration matching measure.yaml shape."""

    evaluator: EvaluatorConfig = field(default_factory=EvaluatorConfig)
    store: StoreConfig = field(default_factory=StoreConfig)
    governance: GovernanceConfig = field(default_factory=GovernanceConfig)
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    dimensions: list[str] = field(default_factory=lambda: ["correctness", "completeness", "safety"])
    agents: list[AgentConfig] = field(default_factory=list)
    verdict: VerdictConfig | None = None
    trigger: TriggerConfig = field(default_factory=TriggerConfig)
    tiering: TieringConfig | None = None


def load_config(path: Path) -> MeasureConfig:
    """Load MeasureConfig from a YAML file."""
    raw = yaml.safe_load(path.read_text())
    if raw is None:
        return MeasureConfig()

    def _section(key: str, cls: type):
        section = raw.get(key)
        if section is None:
            return cls()
        if not isinstance(section, dict):
            raise ValueError(f"Config section '{key}' must be a mapping, got {type(section).__name__}")
        return cls(**section)

    evaluator = _section("evaluator", EvaluatorConfig)
    store = _section("store", StoreConfig)
    governance = _section("governance", GovernanceConfig)
    detection = _section("detection", DetectionConfig)
    dimensions = raw.get("dimensions", ["correctness", "completeness", "safety"])
    if not isinstance(dimensions, list):
        raise ValueError(f"'dimensions' must be a list, got {type(dimensions).__name__}")

    agents = []
    for i, agent_data in enumerate(raw.get("agents", [])):
        if not isinstance(agent_data, dict):
            raise ValueError(f"agents[{i}] must be a mapping, got {type(agent_data).__name__}")
        if "name" not in agent_data:
            raise ValueError(f"agents[{i}] missing required field 'name'")
        agents.append(AgentConfig(
            name=agent_data["name"],
            adapter=agent_data.get("adapter", "webhook"),
            dimensions=agent_data.get("dimensions"),
            manifest=agent_data.get("manifest"),
            adapter_config=agent_data.get("adapter_config", {}),
        ))

    verdict_cfg = None
    verdict_raw = raw.get("verdict")
    if verdict_raw is not None:
        if not isinstance(verdict_raw, dict):
            raise ValueError(f"Config section 'verdict' must be a mapping, got {type(verdict_raw).__name__}")
        store_raw = verdict_raw.get("store", {})
        if not isinstance(store_raw, dict):
            raise ValueError(f"Config section 'verdict.store' must be a mapping, got {type(store_raw).__name__}")
        verdict_cfg = VerdictConfig(store_path=store_raw.get("path", "verdicts.db"))

    trigger_cfg = TriggerConfig()
    trigger_raw = raw.get("trigger")
    if isinstance(trigger_raw, dict):
        corr = trigger_raw.get("correlate", {})
        resp = trigger_raw.get("respond", {})
        trigger_cfg = TriggerConfig(
            correlate_enabled=bool(corr.get("enabled", False)),
            correlate_args=corr.get("args", {}),
            respond_enabled=bool(resp.get("enabled", False)),
            respond_args=resp.get("args", {}),
        )

    tiering_cfg = None
    tiering_raw = raw.get("tiering")
    if isinstance(tiering_raw, dict):
        models = tiering_raw.get("models", {})
        tiering_cfg = TieringConfig(
            enabled=bool(tiering_raw.get("enabled", False)),
            default_tier=str(tiering_raw.get("default_tier", "standard")),
            auto_approve_score=float(tiering_raw.get("auto_approve_score", 1.0)),
            models={**TieringConfig().models, **models},
            sampling_rate=float(tiering_raw.get("sampling_rate", 0.05)),
            sampling_window_size=int(tiering_raw.get("sampling_window_size", 100)),
            quality_threshold=float(tiering_raw.get("quality_threshold", 0.6)),
            promotion_threshold=float(tiering_raw.get("promotion_threshold", 0.10)),
        )

    return MeasureConfig(
        evaluator=evaluator,
        store=store,
        governance=governance,
        detection=detection,
        dimensions=dimensions,
        agents=agents,
        verdict=verdict_cfg,
        trigger=trigger_cfg,
        tiering=tiering_cfg,
    )
