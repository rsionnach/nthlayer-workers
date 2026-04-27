"""Integration tests for tiered evaluation in the pipeline."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from nthlayer_workers.measure.config import TieringConfig
from nthlayer_workers.measure.pipeline.router import PipelineRouter
from nthlayer_workers.measure.tiering.classifier import TierClassifier
from nthlayer_workers.measure.types import AgentOutput, QualityScore


def _make_output(agent="test-agent"):
    return AgentOutput(
        agent_name=agent, task_id="t1",
        output_content="hello", output_type="api",
    )


def _make_score(agent="test-agent", tier=None, auto_approved=False):
    return QualityScore(
        eval_id=str(uuid.uuid4()), agent_name=agent, task_id="t1",
        dimensions={"correctness": 0.9}, confidence=0.85,
        evaluator_model="test-model", tier=tier, auto_approved=auto_approved,
    )


@pytest.fixture
def mock_evaluator():
    e = AsyncMock()
    e.evaluate = AsyncMock(return_value=_make_score(tier="standard"))
    return e


@pytest.fixture
def mock_store():
    s = AsyncMock()
    s.save_score = AsyncMock()
    return s


@pytest.fixture
def mock_tracker():
    return AsyncMock()


@pytest.fixture
def tiering_config():
    return TieringConfig(
        enabled=True,
        default_tier="standard",
        models={
            "standard": "anthropic/claude-haiku-4-20250414",
            "deep": "anthropic/claude-sonnet-4-20250514",
            "critical": "anthropic/claude-opus-4-20250514",
        },
    )


@pytest.mark.asyncio
async def test_router_with_tiering_calls_evaluator_with_model(
    mock_evaluator, mock_store, mock_tracker, tiering_config
):
    classifier = TierClassifier(tiering_config, manifests={})

    async def single_output():
        yield _make_output()

    adapter = AsyncMock()
    adapter.receive = single_output

    router = PipelineRouter(
        adapter=adapter,
        evaluator=mock_evaluator,
        store=mock_store,
        tracker=mock_tracker,
        dimensions=["correctness"],
        classifier=classifier,
    )
    await router.run()

    mock_evaluator.evaluate.assert_called_once()
    call_args = mock_evaluator.evaluate.call_args
    # model= should be passed as keyword argument
    assert call_args.kwargs.get("model") == "anthropic/claude-haiku-4-20250414" or \
           (len(call_args.args) > 2 and call_args.args[2] == "anthropic/claude-haiku-4-20250414")


@pytest.mark.asyncio
async def test_router_minimal_tier_auto_approves(
    mock_evaluator, mock_store, mock_tracker
):
    config = TieringConfig(enabled=True, default_tier="minimal", sampling_rate=0.0)
    classifier = TierClassifier(config, manifests={})

    async def single_output():
        yield _make_output()

    adapter = AsyncMock()
    adapter.receive = single_output

    router = PipelineRouter(
        adapter=adapter,
        evaluator=mock_evaluator,
        store=mock_store,
        tracker=mock_tracker,
        dimensions=["correctness"],
        classifier=classifier,
    )
    await router.run()

    # Evaluator should NOT be called for minimal tier (0% sampling)
    mock_evaluator.evaluate.assert_not_called()
    # But store should have the auto-approved score
    mock_store.save_score.assert_called_once()
    saved_score = mock_store.save_score.call_args[0][0]
    assert saved_score.auto_approved is True
    assert saved_score.tier == "minimal"
    assert saved_score.confidence == 0.0


@pytest.mark.asyncio
async def test_router_without_classifier_unchanged(
    mock_evaluator, mock_store, mock_tracker
):
    """No classifier = original behavior (no tiering)."""
    async def single_output():
        yield _make_output()

    adapter = AsyncMock()
    adapter.receive = single_output

    router = PipelineRouter(
        adapter=adapter,
        evaluator=mock_evaluator,
        store=mock_store,
        tracker=mock_tracker,
        dimensions=["correctness"],
    )
    await router.run()

    mock_evaluator.evaluate.assert_called_once()
