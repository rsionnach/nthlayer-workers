"""Evaluator protocol and implementation — the boundary between transport and judgment.

Uses Instructor via structured_call_with_usage() for validated Pydantic output
with token usage for cost accounting. P3-C.3 replaces raw llm_call() + manual
JSON parsing with structured evaluation.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from nthlayer_common.llm_structured import structured_call_with_usage
from nthlayer_common.prompts import load_prompt, render_user_prompt
from pydantic import BaseModel, Field

from nthlayer_workers.measure.types import AgentOutput, QualityScore

_PROMPT_PATH = Path(__file__).parent.parent.parent.parent / "prompts" / "evaluator.yaml"


# ---------------------------------------------------------------------------
# Pydantic response models for Instructor
# ---------------------------------------------------------------------------


class DimensionScore(BaseModel):
    """Score for a single quality dimension."""

    score: float = Field(ge=0.0, le=1.0)
    reasoning: str = ""


class EvaluationResult(BaseModel):
    """Structured evaluation result from LLM-as-judge."""

    dimensions: dict[str, DimensionScore]
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class Evaluator(Protocol):
    """Evaluates agent output across quality dimensions.

    The evaluator is the boundary where transport hands off to judgment.
    It constructs the prompt, calls the model, and parses the response.
    It never interprets quality itself — that's the model's job (ZFC).
    """

    async def evaluate(self, output: AgentOutput, dimensions: list[str], model: str | None = None) -> QualityScore: ...


# ---------------------------------------------------------------------------
# Cost computation
# ---------------------------------------------------------------------------

# Known model pricing per million tokens (input, output) in USD
_MODEL_PRICING: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-20250514": (3.0, 15.0),
    "claude-haiku-4-20250414": (0.80, 4.0),
    "claude-opus-4-20250514": (15.0, 75.0),
}


def _compute_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Compute cost in USD from token counts. Returns None for unknown models."""
    pricing = _MODEL_PRICING.get(model)
    if pricing is None:
        return None
    input_price, output_price = pricing
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------


class ModelEvaluator:
    """Evaluator that delegates quality judgment to a language model.

    Uses Instructor via structured_call_with_usage() for validated Pydantic
    output with automatic retry on validation failure. Returns both the
    evaluation result and token usage for cost accounting.

    Timeout default: 30s. Configurable.
    """

    def __init__(self, model: str, max_tokens: int = 4096, timeout: float = 30.0) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._timeout = timeout

    def build_prompt(self, output: AgentOutput, dimensions: list[str]) -> str:
        """Construct the evaluation prompt from YAML template."""
        spec = load_prompt(_PROMPT_PATH)
        dimensions_block = "\n".join(f"- {d}" for d in dimensions)
        return render_user_prompt(
            spec.user_template,
            agent_name=output.agent_name,
            task_id=output.task_id,
            output_type=output.output_type,
            output_content=output.output_content,
            dimensions_block=dimensions_block,
        )

    async def evaluate(self, output: AgentOutput, dimensions: list[str], model: str | None = None) -> QualityScore:
        """Evaluate agent output using structured LLM call.

        Returns QualityScore with validated dimensions, confidence, and cost.
        """
        effective_model = model or self._model
        prompt = self.build_prompt(output, dimensions)
        system_prompt = load_prompt(_PROMPT_PATH).system

        result = await asyncio.wait_for(
            asyncio.to_thread(
                structured_call_with_usage,
                system=system_prompt,
                user=prompt,
                response_model=EvaluationResult,
                model=effective_model,
                max_tokens=self._max_tokens,
                timeout=round(self._timeout),
                max_retries=3,
            ),
            # Outer timeout = safety net; inner timeout should fire first.
            timeout=self._timeout + 5.0,
        )

        score = _to_quality_score(result.data, output, effective_model)
        cost = _compute_cost(effective_model, result.usage.input_tokens, result.usage.output_tokens)
        if cost is not None:
            score = replace(score, cost_usd=cost)
        return score


def _to_quality_score(result: EvaluationResult, output: AgentOutput, model: str) -> QualityScore:
    """Convert Pydantic EvaluationResult to QualityScore."""
    return QualityScore(
        eval_id=str(uuid.uuid4()),
        agent_name=output.agent_name,
        task_id=output.task_id,
        dimensions={name: ds.score for name, ds in result.dimensions.items()},
        reasoning={name: ds.reasoning for name, ds in result.dimensions.items() if ds.reasoning},
        confidence=result.confidence,
        evaluator_model=model,
    )
