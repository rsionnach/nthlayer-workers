"""Tests for the async evaluation queue."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nthlayer_workers.measure.api.normalise import EvaluationRequest
from nthlayer_workers.measure.api.queue import EvaluationQueue
from nthlayer_workers.measure.types import QualityScore


def _make_request(agent="test-agent", task_id="task-1", output="hello"):
    return EvaluationRequest(
        agent_name=agent, task_id=task_id, output=output
    )


def _make_score(agent="test-agent", task_id="task-1"):
    return QualityScore(
        eval_id="eval-001",
        agent_name=agent,
        task_id=task_id,
        dimensions={"correctness": 0.9},
        confidence=0.85,
        evaluator_model="test-model",
    )


@pytest.fixture
def mock_evaluator():
    evaluator = AsyncMock()
    evaluator.evaluate = AsyncMock(return_value=_make_score())
    return evaluator


@pytest.fixture
def mock_store():
    store = AsyncMock()
    store.save_score = AsyncMock()
    store.set_verdict_id = AsyncMock()
    return store


@pytest.fixture
async def queue(mock_evaluator, mock_store):
    q = EvaluationQueue(
        evaluator=mock_evaluator,
        store=mock_store,
        dimensions=["correctness"],
        max_workers=2,
    )
    await q.start()
    yield q
    await q.stop()


@pytest.mark.asyncio
async def test_submit_returns_eval_id(queue):
    eval_id = await queue.submit(_make_request())
    assert eval_id.startswith("eval-")


@pytest.mark.asyncio
async def test_result_complete_after_processing(queue, mock_evaluator):
    eval_id = await queue.submit(_make_request())
    # Wait for processing
    await asyncio.sleep(0.1)
    result = await queue.get_result(eval_id)
    assert result["status"] == "complete"
    assert result["score"].eval_id == "eval-001"
    mock_evaluator.evaluate.assert_called_once()


@pytest.mark.asyncio
async def test_not_found_for_unknown_id(queue):
    result = await queue.get_result("eval-nonexistent")
    assert result["status"] == "not_found"


@pytest.mark.asyncio
async def test_error_on_evaluator_failure(mock_store):
    failing_evaluator = AsyncMock()
    failing_evaluator.evaluate = AsyncMock(
        side_effect=Exception("Model down")
    )
    q = EvaluationQueue(
        evaluator=failing_evaluator,
        store=mock_store,
        dimensions=["correctness"],
        max_workers=1,
    )
    await q.start()
    try:
        eval_id = await q.submit(_make_request())
        await asyncio.sleep(0.1)
        result = await q.get_result(eval_id)
        assert result["status"] == "error"
        assert "Model down" in result["error"]
    finally:
        await q.stop()


@pytest.mark.asyncio
async def test_verdict_created_when_store_provided(mock_evaluator, mock_store):
    verdict_store = MagicMock()
    verdict_store.put = MagicMock()

    q = EvaluationQueue(
        evaluator=mock_evaluator,
        store=mock_store,
        dimensions=["correctness"],
        verdict_store=verdict_store,
        max_workers=1,
    )
    await q.start()
    try:
        eval_id = await q.submit(_make_request())
        await asyncio.sleep(0.2)
        result = await q.get_result(eval_id)
        assert result["status"] == "complete"
        assert result["verdict"] is not None
        verdict_store.put.assert_called_once()
    finally:
        await q.stop()


@pytest.mark.asyncio
async def test_callback_fires_on_completion(mock_evaluator, mock_store):
    q = EvaluationQueue(
        evaluator=mock_evaluator,
        store=mock_store,
        dimensions=["correctness"],
        max_workers=1,
    )
    await q.start()
    try:
        req = _make_request()
        req.callback_url = "https://example.com/callback"

        with patch("nthlayer_workers.measure.api.queue.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await q.submit(req)
            await asyncio.sleep(0.2)

            mock_client.post.assert_called_once()
            call_args = mock_client.post.call_args
            assert call_args[0][0] == "https://example.com/callback"
    finally:
        await q.stop()
