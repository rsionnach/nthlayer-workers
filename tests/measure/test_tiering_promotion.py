"""Tests for tier promotion ratchet."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from nthlayer_workers.measure.config import TieringConfig
from nthlayer_workers.measure.tiering.promotion import TierPromotionChecker
from nthlayer_workers.measure.types import QualityScore


def _make_sampled_score(agent="test-agent", dim_score=0.9):
    return QualityScore(
        eval_id=str(uuid.uuid4()), agent_name=agent, task_id="t1",
        dimensions={"correctness": dim_score}, confidence=0.85,
        evaluator_model="test-model", tier="minimal", auto_approved=False,
    )


@pytest.fixture
def config():
    return TieringConfig(
        enabled=True,
        sampling_window_size=5,
        quality_threshold=0.6,
        promotion_threshold=0.40,
    )


@pytest.fixture
def mock_store():
    return AsyncMock()


@pytest.fixture
def mock_verdict_store():
    v = MagicMock()
    v.put = MagicMock()
    return v


@pytest.mark.asyncio
async def test_no_promotion_when_samples_pass(config, mock_store, mock_verdict_store):
    scores = [_make_sampled_score(dim_score=0.9) for _ in range(5)]
    mock_store.get_scores = AsyncMock(return_value=scores)

    checker = TierPromotionChecker(mock_store, mock_verdict_store, config, manifests={})
    result = await checker.check_agent("test-agent")
    assert result is None


@pytest.mark.asyncio
async def test_promotion_when_samples_fail(config, mock_store, mock_verdict_store):
    scores = [
        _make_sampled_score(dim_score=0.3),
        _make_sampled_score(dim_score=0.4),
        _make_sampled_score(dim_score=0.2),
        _make_sampled_score(dim_score=0.9),
        _make_sampled_score(dim_score=0.8),
    ]
    mock_store.get_scores = AsyncMock(return_value=scores)

    checker = TierPromotionChecker(mock_store, mock_verdict_store, config, manifests={})
    result = await checker.check_agent("test-agent")
    assert result is not None
    assert result.from_tier == "minimal"
    assert result.to_tier == "standard"
    mock_verdict_store.put.assert_called_once()


@pytest.mark.asyncio
async def test_no_promotion_below_window_size(config, mock_store, mock_verdict_store):
    scores = [_make_sampled_score(dim_score=0.1) for _ in range(3)]
    mock_store.get_scores = AsyncMock(return_value=scores)

    checker = TierPromotionChecker(mock_store, mock_verdict_store, config, manifests={})
    result = await checker.check_agent("test-agent")
    assert result is None


@pytest.mark.asyncio
async def test_manifest_threshold_override(mock_store, mock_verdict_store):
    config = TieringConfig(enabled=True, sampling_window_size=5, quality_threshold=0.6, promotion_threshold=0.80)
    manifests = {"test-agent": {"promotion_threshold": 0.20}}

    scores = [
        _make_sampled_score(dim_score=0.3),
        _make_sampled_score(dim_score=0.9),
        _make_sampled_score(dim_score=0.9),
        _make_sampled_score(dim_score=0.9),
        _make_sampled_score(dim_score=0.9),
    ]
    mock_store.get_scores = AsyncMock(return_value=scores)

    checker = TierPromotionChecker(mock_store, mock_verdict_store, config, manifests=manifests)
    result = await checker.check_agent("test-agent")
    # 1/5 = 20% failure rate. Manifest threshold is 0.20. 0.20 > 0.20 is False → no promotion
    assert result is None
