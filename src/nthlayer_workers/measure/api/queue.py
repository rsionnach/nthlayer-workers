"""Async evaluation queue for fire-and-forget API requests.

Not a message broker — just an asyncio queue within the server process.
Evaluations are processed by a pool of async workers.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections import OrderedDict
from typing import Any

import httpx

from nthlayer_workers.measure.api.normalise import EvaluationRequest
from nthlayer_workers.measure.pipeline.evaluator import Evaluator
from nthlayer_workers.measure.store.protocol import ScoreStore
from nthlayer_workers.measure.tiering.classifier import TierClassifier
from nthlayer_workers.measure.types import AgentOutput, QualityScore

logger = logging.getLogger(__name__)

DEFAULT_APPROVE_THRESHOLD = 0.5
MAX_RESULTS = 10_000  # Evict oldest results beyond this limit


class EvaluationQueue:
    """Async queue that processes evaluation requests in the background."""

    def __init__(
        self,
        evaluator: Evaluator,
        store: ScoreStore,
        dimensions: list[str],
        verdict_store=None,
        approve_threshold: float = DEFAULT_APPROVE_THRESHOLD,
        max_workers: int = 5,
        classifier: TierClassifier | None = None,
    ) -> None:
        self._evaluator = evaluator
        self._store = store
        self._dimensions = dimensions
        self._verdict_store = verdict_store
        self._approve_threshold = approve_threshold
        self._max_workers = max_workers
        self._classifier = classifier
        self._queue: asyncio.Queue = asyncio.Queue()
        self._results: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._workers: list[asyncio.Task] = []

    async def start(self) -> None:
        """Spawn worker tasks."""
        for _ in range(self._max_workers):
            task = asyncio.create_task(self._worker())
            self._workers.append(task)

    async def stop(self) -> None:
        """Drain queue and cancel workers."""
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    async def submit(self, request: EvaluationRequest) -> str:
        """Submit an evaluation request. Returns eval_id immediately."""
        eval_id = f"eval-{uuid.uuid4().hex[:12]}"
        self._results[eval_id] = {"status": "queued"}
        # Evict oldest results to prevent unbounded memory growth
        while len(self._results) > MAX_RESULTS:
            self._results.popitem(last=False)
        await self._queue.put((eval_id, request))
        return eval_id

    async def get_result(self, eval_id: str) -> dict[str, Any]:
        """Get result by eval_id. Returns status dict."""
        return self._results.get(eval_id, {"status": "not_found"})

    async def _worker(self) -> None:
        """Process queued evaluations."""
        while True:
            eval_id, request = await self._queue.get()
            try:
                self._results[eval_id] = {"status": "evaluating"}

                agent_output = AgentOutput(
                    agent_name=request.agent_name,
                    task_id=request.task_id,
                    output_content=request.output,
                    output_type="api",
                    metadata=request.metadata,
                )

                # Tier classification (if enabled)
                tier = None
                model_override = None
                if self._classifier is not None:
                    tier = self._classifier.classify(agent_output, agent_output.metadata)

                    if tier == "minimal" and not self._classifier.should_sample(tier, agent_output.agent_name):
                        # Auto-approve: skip model call entirely
                        score = QualityScore(
                            eval_id=str(uuid.uuid4()),
                            agent_name=agent_output.agent_name,
                            task_id=agent_output.task_id,
                            dimensions={d: self._classifier._config.auto_approve_score for d in self._dimensions},
                            confidence=0.0,
                            evaluator_model="auto-approved",
                            tier="minimal",
                            auto_approved=True,
                        )
                        await self._store.save_score(score)
                        self._results[eval_id] = {
                            "status": "complete",
                            "score": score,
                            "verdict": None,
                        }
                        continue

                    # Model routing for non-minimal tiers (or sampled minimal)
                    model_override = self._classifier._config.models.get(tier)

                score = await self._evaluator.evaluate(
                    agent_output, self._dimensions, model=model_override
                )

                # Tag score with tier
                if tier is not None:
                    from dataclasses import replace
                    score = replace(score, tier=tier)

                await self._store.save_score(score)

                # Create verdict (fail-open, matches PipelineRouter pattern)
                verdict = None
                if self._verdict_store is not None:
                    try:
                        verdict = await self._create_verdict(score)
                        await asyncio.to_thread(
                            self._verdict_store.put, verdict
                        )
                        await self._store.set_verdict_id(
                            score.eval_id, verdict.id
                        )
                    except Exception:
                        logger.warning(
                            "Failed to create verdict for %s",
                            eval_id,
                            exc_info=True,
                        )

                self._results[eval_id] = {
                    "status": "complete",
                    "score": score,
                    "verdict": verdict,
                }

                # Fire callback if provided
                if request.callback_url:
                    await self._send_callback(
                        request.callback_url, eval_id, score, verdict
                    )

            except Exception as exc:
                logger.warning(
                    "Evaluation failed for %s: %s", eval_id, exc
                )
                self._results[eval_id] = {
                    "status": "error",
                    "error": str(exc),
                }
            finally:
                self._queue.task_done()

    async def _create_verdict(self, score):
        """Create a verdict from a QualityScore. Mirrors PipelineRouter._create_verdict."""
        from nthlayer_common.verdicts import create as verdict_create

        dims = score.dimensions or {}
        avg_score = sum(dims.values()) / len(dims) if dims else 0.0

        reasoning_summary = (
            "; ".join(f"{k}: {v}" for k, v in score.reasoning.items())
            if score.reasoning
            else None
        )

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
                    "approve"
                    if avg_score >= self._approve_threshold
                    else "reject"
                ),
                "confidence": score.confidence,
                "score": avg_score,
                "dimensions": score.dimensions,
                "reasoning": reasoning_summary,
            },
            producer={
                "system": "nthlayer-measure",
                "model": score.evaluator_model,
            },
            metadata={"cost_currency": score.cost_usd},
        )

    async def _send_callback(
        self, url: str, eval_id: str, score, verdict
    ) -> None:
        """POST verdict to callback URL. Best-effort with 3 retries."""
        from nthlayer_workers.measure.api.response import build_response

        payload = (
            build_response(verdict)
            if verdict
            else {"eval_id": eval_id, "status": "complete"}
        )
        payload["evaluation_id"] = eval_id

        async with httpx.AsyncClient() as client:
            for attempt in range(3):
                try:
                    resp = await client.post(url, json=payload, timeout=10.0)
                    resp.raise_for_status()
                    return
                except httpx.HTTPStatusError as exc:
                    # Don't retry 4xx (permanent errors)
                    if 400 <= exc.response.status_code < 500:
                        logger.warning("Callback rejected (HTTP %d): %s", exc.response.status_code, url)
                        return
                except Exception:
                    pass
                if attempt < 2:
                    await asyncio.sleep(1 * (2 ** attempt))  # 1s, 2s backoff
                else:
                    logger.warning("Callback failed after 3 attempts: %s", url)
