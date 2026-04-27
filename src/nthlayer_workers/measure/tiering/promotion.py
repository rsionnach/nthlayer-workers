"""Tier promotion ratchet — one-way safety mechanism for evaluation tiers."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from nthlayer_workers.measure.config import TieringConfig
from nthlayer_workers.measure.store.protocol import ScoreStore

logger = logging.getLogger(__name__)


@dataclass
class TierPromotion:
    """Result of a tier promotion check."""

    agent_name: str
    from_tier: str
    to_tier: str
    failure_rate: float
    threshold: float
    sample_count: int
    failed_count: int


class TierPromotionChecker:
    """Checks whether minimal-tier agents should be promoted to standard.

    One-way ratchet: can promote (minimal → standard), never demote.
    Human CLI command required to restore minimal tier.
    """

    def __init__(
        self,
        store: ScoreStore,
        verdict_store: Any,
        config: TieringConfig,
        manifests: dict[str, dict],
    ) -> None:
        self._store = store
        self._verdict_store = verdict_store
        self._config = config
        self._manifests = manifests

    async def check_agent(self, agent_name: str) -> TierPromotion | None:
        """Check if agent should be promoted from minimal to standard."""
        scores = await self._store.get_scores(agent_name, since=None, limit=self._config.sampling_window_size)

        # Filter to sampled minimal-tier evaluations (not auto-approved)
        sampled = [s for s in scores if getattr(s, "tier", None) == "minimal" and not getattr(s, "auto_approved", True)]

        if len(sampled) < self._config.sampling_window_size:
            return None

        # Count failures: any dimension below quality_threshold
        failed = 0
        for s in sampled:
            if any(v < self._config.quality_threshold for v in s.dimensions.values()):
                failed += 1

        failure_rate = failed / len(sampled)

        # Check threshold (manifest override takes priority)
        manifest = self._manifests.get(agent_name, {})
        threshold = manifest.get("promotion_threshold", self._config.promotion_threshold)

        if failure_rate <= threshold:
            return None

        promotion = TierPromotion(
            agent_name=agent_name,
            from_tier="minimal",
            to_tier="standard",
            failure_rate=failure_rate,
            threshold=threshold,
            sample_count=len(sampled),
            failed_count=failed,
        )

        self._emit_promotion_verdict(promotion)
        return promotion

    def _emit_promotion_verdict(self, promotion: TierPromotion) -> None:
        """Emit a verdict recording the tier promotion."""
        if self._verdict_store is None:
            return

        from nthlayer_common.verdicts import create as verdict_create

        verdict = verdict_create(
            subject={
                "type": "evaluation",
                "ref": promotion.agent_name,
                "summary": (
                    f"{promotion.agent_name} promoted from {promotion.from_tier} to {promotion.to_tier} tier. "
                    f"{promotion.failed_count} of {promotion.sample_count} sampled auto-approvals would have been flagged "
                    f"({promotion.failure_rate:.0%}, threshold {promotion.threshold:.0%}). "
                    f"Human review required to restore {promotion.from_tier} tier."
                ),
            },
            judgment={
                "action": "escalate",
                "confidence": 1.0,
                "reasoning": (
                    f"Tier promotion ratchet triggered for {promotion.agent_name}. "
                    f"Sample failure rate {promotion.failure_rate:.0%} exceeds threshold {promotion.threshold:.0%}."
                ),
                "tags": ["tier_promotion", "calibration"],
            },
            producer={"system": "nthlayer-measure"},
        )
        self._verdict_store.put(verdict)
        logger.warning(
            "Tier promotion: %s promoted from %s to %s (failure rate %.0f%%, threshold %.0f%%)",
            promotion.agent_name, promotion.from_tier, promotion.to_tier,
            promotion.failure_rate * 100, promotion.threshold * 100,
        )
