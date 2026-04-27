"""FastAPI HTTP API server for nthlayer-measure.

Wraps the existing evaluation pipeline with a universal HTTP API.
Any system that can make an HTTP POST can integrate.
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from nthlayer_workers.measure.api.normalise import normalise_input
from nthlayer_workers.measure.api.queue import EvaluationQueue
from nthlayer_workers.measure.api.response import build_error_response, build_response
from nthlayer_workers.measure.pipeline.evaluator import Evaluator
from nthlayer_workers.measure.store.protocol import ScoreStore
from nthlayer_workers.measure.tiering.classifier import TierClassifier
from nthlayer_workers.measure.trends.tracker import TrendTracker
from nthlayer_workers.measure.types import AgentOutput, QualityScore

logger = logging.getLogger(__name__)


def create_app(
    evaluator: Evaluator,
    store: ScoreStore,
    tracker: TrendTracker,
    dimensions: list[str],
    governance=None,
    verdict_store=None,
    approve_threshold: float = 0.5,
    sync_timeout: float = 30.0,
    max_workers: int = 5,
    cors_origins: list[str] | None = None,
    classifier: TierClassifier | None = None,
) -> FastAPI:
    """Create a configured FastAPI application.

    Components are injected via closure — no FastAPI Depends.
    """
    app = FastAPI(
        title="nthlayer-measure API",
        description="Universal quality measurement API for AI agent output",
        version="0.1.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins or ["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    queue = EvaluationQueue(
        evaluator=evaluator,
        store=store,
        dimensions=dimensions,
        verdict_store=verdict_store,
        approve_threshold=approve_threshold,
        max_workers=max_workers,
        classifier=classifier,
    )

    @asynccontextmanager
    async def lifespan(app):
        await queue.start()
        yield
        await queue.stop()

    app.router.lifespan_context = lifespan

    # ------------------------------------------------------------------ #
    # Health                                                               #
    # ------------------------------------------------------------------ #

    @app.get("/api/v1/health")
    async def health():
        return {"status": "ok"}

    # ------------------------------------------------------------------ #
    # Level 1: Fire and forget                                             #
    # ------------------------------------------------------------------ #

    async def _parse_json(request: Request) -> dict | JSONResponse:
        """Parse JSON body, returning error response on failure."""
        try:
            return await request.json()
        except Exception:
            return JSONResponse(
                status_code=422,
                content=build_error_response(422, "Invalid JSON in request body"),
            )

    @app.post("/api/v1/evaluate", status_code=202)
    async def evaluate_async(request: Request):
        body = await _parse_json(request)
        if isinstance(body, JSONResponse):
            return body
        try:
            eval_req = normalise_input(body)
        except ValueError as exc:
            return JSONResponse(
                status_code=422,
                content=build_error_response(422, str(exc)),
            )

        eval_id = await queue.submit(eval_req)
        return {
            "evaluation_id": eval_id,
            "status": "queued",
            "poll_url": f"/api/v1/evaluations/{eval_id}",
        }

    # ------------------------------------------------------------------ #
    # Level 2: Synchronous gate                                            #
    # ------------------------------------------------------------------ #

    @app.post("/api/v1/evaluate/sync")
    async def evaluate_sync(request: Request):
        body = await _parse_json(request)
        if isinstance(body, JSONResponse):
            return body
        try:
            eval_req = normalise_input(body)
        except ValueError as exc:
            return JSONResponse(
                status_code=422,
                content=build_error_response(422, str(exc)),
            )

        agent_output = AgentOutput(
            agent_name=eval_req.agent_name,
            task_id=eval_req.task_id,
            output_content=eval_req.output,
            output_type="api",
            metadata=eval_req.metadata,
        )

        # Tier classification (if enabled)
        tier = None
        model_override = None
        if classifier is not None:
            tier = classifier.classify(agent_output, agent_output.metadata)

            if tier == "minimal" and not classifier.should_sample(tier, agent_output.agent_name):
                # Auto-approve: skip model call entirely
                auto_score = QualityScore(
                    eval_id=str(uuid.uuid4()),
                    agent_name=agent_output.agent_name,
                    task_id=agent_output.task_id,
                    dimensions={d: classifier._config.auto_approve_score for d in dimensions},
                    confidence=0.0,
                    evaluator_model="auto-approved",
                    tier="minimal",
                    auto_approved=True,
                )
                await store.save_score(auto_score)
                return {
                    "eval_id": auto_score.eval_id,
                    "action": "approve",
                    "dimensions": auto_score.dimensions,
                    "confidence": auto_score.confidence,
                    "tier": "minimal",
                    "auto_approved": True,
                }

            # Model routing for non-minimal tiers (or sampled minimal)
            model_override = classifier._config.models.get(tier)

        try:
            score = await asyncio.wait_for(
                evaluator.evaluate(agent_output, dimensions, model=model_override),
                timeout=sync_timeout,
            )
        except asyncio.TimeoutError:
            return JSONResponse(
                status_code=408,
                content={
                    "status": "timeout",
                    "message": f"Evaluation did not complete within {sync_timeout:.0f}s. Retry with POST /api/v1/evaluate for async processing.",
                },
            )

        # Tag score with tier
        if tier is not None:
            from dataclasses import replace
            score = replace(score, tier=tier)

        await store.save_score(score)

        # Create verdict (fail-open)
        verdict = None
        if verdict_store is not None:
            try:
                verdict = await queue._create_verdict(score)
                await asyncio.to_thread(verdict_store.put, verdict)
                await store.set_verdict_id(score.eval_id, verdict.id)
            except Exception:
                logger.warning("Failed to create verdict for sync eval", exc_info=True)

        # Governance status (optional)
        gov_data = None
        if governance is not None and verdict is not None:
            try:
                level = await governance.get_autonomy(eval_req.agent_name)
                window = await tracker.compute_window(eval_req.agent_name, 7)
                gov_data = {
                    "agent_status": level.value,
                    "reversal_rate": window.reversal_rate,
                }
            except Exception:
                logger.warning("Failed to fetch governance data", exc_info=True)

        if verdict is not None:
            return build_response(verdict, governance=gov_data)

        # No verdict store — return score directly
        return {
            "eval_id": score.eval_id,
            "action": "approve" if (sum(score.dimensions.values()) / max(len(score.dimensions), 1)) >= approve_threshold else "reject",
            "dimensions": score.dimensions,
            "confidence": score.confidence,
            "reasoning": score.reasoning,
        }

    # ------------------------------------------------------------------ #
    # Poll for async result                                                #
    # ------------------------------------------------------------------ #

    @app.get("/api/v1/evaluations/{eval_id}")
    async def get_evaluation(eval_id: str):
        result = await queue.get_result(eval_id)
        if result["status"] == "not_found":
            return JSONResponse(
                status_code=404,
                content=build_error_response(404, f"Evaluation {eval_id} not found"),
            )
        if result["status"] == "complete":
            if result.get("verdict"):
                return {
                    "status": "complete",
                    **build_response(result["verdict"]),
                }
            # Verdict creation failed (fail-open) — return score summary
            score = result.get("score")
            if score:
                return {
                    "status": "complete",
                    "eval_id": score.eval_id,
                    "dimensions": score.dimensions,
                    "confidence": score.confidence,
                }
        # queued, evaluating, or error
        if result.get("score"):
            # Strip non-serializable QualityScore from response
            return {k: v for k, v in result.items() if k != "score"}
        return result

    # ------------------------------------------------------------------ #
    # Override and confirm (human feedback loop)                            #
    # ------------------------------------------------------------------ #

    @app.post("/api/v1/override")
    async def override_verdict(request: Request):
        if verdict_store is None:
            return JSONResponse(
                status_code=503,
                content=build_error_response(503, "Verdict store not configured"),
            )

        body = await _parse_json(request)
        if isinstance(body, JSONResponse):
            return body
        verdict_id = body.get("verdict_id")
        actor = body.get("actor")
        if not verdict_id or not actor:
            return JSONResponse(
                status_code=422,
                content=build_error_response(422, "Missing required fields: verdict_id, actor"),
            )

        try:
            await asyncio.to_thread(
                verdict_store.resolve,
                verdict_id,
                "overridden",
                override={"by": actor, "reasoning": body.get("reasoning", "")},
            )
        except KeyError:
            return JSONResponse(
                status_code=404,
                content=build_error_response(404, f"Verdict {verdict_id} not found"),
            )
        except ValueError as exc:
            return JSONResponse(
                status_code=409,
                content=build_error_response(409, str(exc)),
            )

        return {"verdict_id": verdict_id, "status": "overridden"}

    @app.post("/api/v1/confirm")
    async def confirm_verdict(request: Request):
        if verdict_store is None:
            return JSONResponse(
                status_code=503,
                content=build_error_response(503, "Verdict store not configured"),
            )

        body = await _parse_json(request)
        if isinstance(body, JSONResponse):
            return body
        verdict_id = body.get("verdict_id")
        actor = body.get("actor")
        if not verdict_id or not actor:
            return JSONResponse(
                status_code=422,
                content=build_error_response(422, "Missing required fields: verdict_id, actor"),
            )

        try:
            reasoning = body.get("reasoning")
            await asyncio.to_thread(
                verdict_store.resolve,
                verdict_id,
                "confirmed",
                resolution=reasoning,
            )
        except KeyError:
            return JSONResponse(
                status_code=404,
                content=build_error_response(404, f"Verdict {verdict_id} not found"),
            )
        except ValueError as exc:
            return JSONResponse(
                status_code=409,
                content=build_error_response(409, str(exc)),
            )

        return {"verdict_id": verdict_id, "status": "confirmed"}

    @app.post("/api/v1/resolve/batch")
    async def resolve_batch(request: Request):
        if verdict_store is None:
            return JSONResponse(
                status_code=503,
                content=build_error_response(503, "Verdict store not configured"),
            )

        body = await _parse_json(request)
        if isinstance(body, JSONResponse):
            return body
        resolutions = body.get("resolutions", [])
        if len(resolutions) > 100:
            return JSONResponse(
                status_code=422,
                content=build_error_response(422, f"Batch too large: {len(resolutions)} items (max 100)"),
            )
        results = []

        for item in resolutions:
            vid = item.get("verdict_id")
            status = item.get("status")
            actor = item.get("actor", "")

            if not vid or not status:
                results.append({"verdict_id": vid, "status": "error", "error": "Missing required fields: verdict_id, status"})
                continue

            try:
                if status == "overridden":
                    await asyncio.to_thread(
                        verdict_store.resolve,
                        vid, "overridden",
                        override={"by": actor, "reasoning": item.get("reasoning", "")},
                    )
                elif status == "confirmed":
                    await asyncio.to_thread(
                        verdict_store.resolve, vid, "confirmed",
                        resolution=item.get("reasoning"),
                    )
                else:
                    results.append({"verdict_id": vid, "status": "error", "error": f"Unknown status: {status}"})
                    continue
                results.append({"verdict_id": vid, "status": status})
            except (KeyError, ValueError) as exc:
                results.append({"verdict_id": vid, "status": "error", "error": str(exc)})

        return {"results": results}

    # ------------------------------------------------------------------ #
    # Query endpoints                                                      #
    # ------------------------------------------------------------------ #

    @app.get("/api/v1/agents/{agent_name}/accuracy")
    async def agent_accuracy(agent_name: str, window: str = "30d"):
        if verdict_store is None:
            return JSONResponse(
                status_code=503,
                content=build_error_response(503, "Verdict store not configured"),
            )

        from_time = _parse_window(window)

        from nthlayer_common.verdicts import AccuracyFilter
        report = verdict_store.accuracy(AccuracyFilter(
            producer_system="nthlayer-measure",
            from_time=from_time,
        ))

        result: dict[str, Any] = {
            "agent": agent_name,
            "window": window,
            "total_verdicts": report.total,
            "total_resolved": report.total_resolved,
            "confirmation_rate": report.confirmation_rate,
            "override_rate": report.override_rate,
            "pending_rate": report.pending_rate,
        }

        # Add governance if available
        if governance is not None:
            try:
                level = await governance.get_autonomy(agent_name)
                result["governance"] = {"status": level.value}
            except Exception:
                pass

        return result

    @app.get("/api/v1/agents/{agent_name}/verdicts")
    async def agent_verdicts(
        agent_name: str, limit: int = 20, status: str | None = None
    ):
        if verdict_store is None:
            return JSONResponse(
                status_code=503,
                content=build_error_response(503, "Verdict store not configured"),
            )

        from nthlayer_common.verdicts import VerdictFilter
        kwargs: dict[str, Any] = {
            "producer_system": "nthlayer-measure",
            "subject_agent": agent_name,
            "limit": limit,
        }
        if status and status != "all":
            kwargs["status"] = status

        verdicts = verdict_store.query(VerdictFilter(**kwargs))
        return {"verdicts": [build_response(v) for v in verdicts]}

    @app.get("/api/v1/governance/{agent_name}")
    async def governance_status(agent_name: str):
        if governance is None:
            return JSONResponse(
                status_code=503,
                content=build_error_response(503, "Governance not configured"),
            )

        try:
            level = await governance.get_autonomy(agent_name)
            window = await tracker.compute_window(agent_name, 7)
        except Exception:
            logger.warning("Failed to fetch governance data for %s", agent_name, exc_info=True)
            return JSONResponse(
                status_code=503,
                content=build_error_response(503, "Failed to fetch governance data"),
            )

        return {
            "agent": agent_name,
            "status": level.value,
            "reversal_rate": window.reversal_rate,
            "evaluation_count": window.evaluation_count,
            "confidence_mean": window.confidence_mean,
        }

    return app


def _parse_window(window_str: str) -> datetime:
    """Parse window string like '30d', '7d', '24h' to a from_time datetime."""
    match = re.match(r"^(\d+)([dhwm])$", window_str)
    if not match:
        return datetime.now(timezone.utc) - timedelta(days=30)

    value = int(match.group(1))
    unit = match.group(2)
    delta = {
        "d": timedelta(days=value),
        "h": timedelta(hours=value),
        "w": timedelta(weeks=value),
        "m": timedelta(days=value * 30),
    }[unit]
    return datetime.now(timezone.utc) - delta
