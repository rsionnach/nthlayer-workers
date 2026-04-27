"""Tier classification for evaluation inputs. Pure transport — no model calls."""
from __future__ import annotations

import random

from nthlayer_workers.measure.config import TieringConfig
from nthlayer_workers.measure.types import AgentOutput

VALID_TIERS = {"minimal", "standard", "deep", "critical"}


class TierClassifier:
    """Determines evaluation tier for an agent output.

    Resolution order: caller override → manifest → config default → "standard".
    """

    def __init__(
        self,
        config: TieringConfig,
        manifests: dict[str, dict],
    ) -> None:
        self._config = config
        self._manifests = manifests

    def classify(
        self,
        output: AgentOutput,
        metadata: dict | None = None,
    ) -> str:
        """Returns tier: 'minimal', 'standard', 'deep', 'critical'."""
        # 1. Caller override (highest priority)
        if metadata and metadata.get("risk_tier") in VALID_TIERS:
            return metadata["risk_tier"]

        # 2. Manifest default for this agent
        manifest = self._manifests.get(output.agent_name, {})
        manifest_tier = manifest.get("tier")
        if manifest_tier in VALID_TIERS:
            return manifest_tier

        # 3. Config default
        if self._config.default_tier in VALID_TIERS:
            return self._config.default_tier

        # 4. Fallback
        return "standard"

    def should_sample(self, tier: str, agent_name: str) -> bool:
        """Returns True if this minimal-tier output should be sampled."""
        if tier != "minimal":
            return False

        # Check manifest override for sampling rate
        manifest = self._manifests.get(agent_name, {})
        rate = manifest.get("sampling_rate", self._config.sampling_rate)

        return random.random() < rate
