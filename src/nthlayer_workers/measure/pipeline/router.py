"""Pipeline router — connects adapters to evaluators to stores."""

from __future__ import annotations

import asyncio
import uuid

from nthlayer_workers.measure.adapters.protocol import Adapter
from nthlayer_workers.measure.detection.protocol import DegradationDetector
from nthlayer_workers.measure.governance.engine import GovernanceEngine
from nthlayer_workers.measure.pipeline.evaluator import Evaluator
from nthlayer_workers.measure.store.protocol import ScoreStore
from nthlayer_workers.measure.telemetry import emit_decision_event
from nthlayer_workers.measure.tiering.classifier import TierClassifier
from nthlayer_workers.measure.trends.tracker import TrendTracker
from nthlayer_workers.measure.types import QualityScore
from nthlayer_common.verdicts import create as verdict_create, VerdictStore as VerdictStoreBase

import logging

logger = logging.getLogger(__name__)

DEFAULT_APPROVE_THRESHOLD = 0.5


class PipelineRouter:
    """Routes agent output through the evaluation pipeline.

    Flow: adapter.receive() -> evaluator.evaluate() -> store.save_score()
          -> governance.check_agent() -> detector.check() -> (alerts)
    """

    def __init__(
        self,
        adapter: Adapter,
        evaluator: Evaluator,
        store: ScoreStore,
        tracker: TrendTracker,
        dimensions: list[str],
        governance: GovernanceEngine | None = None,
        detector: DegradationDetector | None = None,
        detection_window_days: int = 7,
        verdict_store: VerdictStoreBase | None = None,
        approve_threshold: float | None = None,
        classifier: TierClassifier | None = None,
    ) -> None:
        self._adapter = adapter
        self._evaluator = evaluator
        self._store = store
        self._tracker = tracker
        self._dimensions = dimensions
        self._governance = governance
        self._detector = detector
        self._detection_window_days = detection_window_days
        self._verdict_store = verdict_store
        self._approve_threshold = (
            approve_threshold if approve_threshold is not None
            else DEFAULT_APPROVE_THRESHOLD
        )
        self._classifier = classifier

    async def run(self) -> None:
        """Process agent outputs through the full pipeline."""
        async for output in self._adapter.receive():
            # Tier classification (if enabled)
            tier = None
            model_override = None
            if self._classifier is not None:
                tier = self._classifier.classify(output, output.metadata)

                if tier == "minimal" and not self._classifier.should_sample(tier, output.agent_name):
                    # Auto-approve: skip model call
                    auto_score = QualityScore(
                        eval_id=str(uuid.uuid4()),
                        agent_name=output.agent_name,
                        task_id=output.task_id,
                        dimensions={d: self._classifier._config.auto_approve_score for d in self._dimensions},
                        confidence=0.0,
                        evaluator_model="auto-approved",
                        tier="minimal",
                        auto_approved=True,
                    )
                    await self._store.save_score(auto_score)
                    emit_decision_event(auto_score, None)
                    continue

                # Model routing for non-minimal tiers (or sampled minimal)
                model_override = self._classifier._config.models.get(tier)

            score = await self._evaluator.evaluate(output, self._dimensions, model=model_override)

            # Tag score with tier
            if tier is not None:
                from dataclasses import replace
                score = replace(score, tier=tier)

            await self._store.save_score(score)

            # Create verdict if verdict store is configured (fail open)
            if self._verdict_store is not None:
                try:
                    verdict = await self._create_verdict(score)
                    await asyncio.to_thread(self._verdict_store.put, verdict)
                    await self._store.set_verdict_id(score.eval_id, verdict.id)
                except Exception:
                    logger.warning(
                        "Failed to create/store verdict for %s — continuing without verdict",
                        score.eval_id,
                        exc_info=True,
                    )

            alerts = None
            if self._detector is not None:
                window = await self._tracker.compute_window(
                    output.agent_name, self._detection_window_days
                )
                alerts = self._detector.check(window)

            emit_decision_event(score, alerts)

            if self._governance is not None:
                await self._governance.check_agent(output.agent_name)

    async def _create_verdict(self, score):
        """Map QualityScore to a verdict."""
        if not score.dimensions:
            avg_score = 0.0
        else:
            avg_score = sum(score.dimensions.values()) / len(score.dimensions)

        reasoning_summary = "; ".join(
            f"{name}: {reason}" for name, reason in score.reasoning.items()
        ) if score.reasoning else None

        return await asyncio.to_thread(
            verdict_create,
            subject={
                "type": "agent_output",
                "ref": score.task_id,
                "summary": f"Evaluation of {score.agent_name}: {score.task_id}",
                "agent": score.agent_name,
            },
            judgment={
                "action": (
                    "approve" if avg_score >= self._approve_threshold
                    else "reject"
                ),
                "confidence": score.confidence,
                "score": avg_score,
                "dimensions": score.dimensions,
                "reasoning": reasoning_summary,
            },
            producer={
                "system": "arbiter",
                "model": score.evaluator_model,
            },
            metadata={
                "cost_currency": score.cost_usd,
            },
        )
