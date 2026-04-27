"""Tests for ErrorBudgetGovernance — model-based judgment + safety ratchet enforcement."""

import json
import pytest
import pytest_asyncio
from unittest.mock import MagicMock, patch

from nthlayer_workers.measure.store.sqlite import SQLiteScoreStore
from nthlayer_workers.measure.governance.engine import ErrorBudgetGovernance
from nthlayer_workers.measure.trends.tracker import StoreTrendTracker
from nthlayer_workers.measure.types import AutonomyLevel, QualityScore


@pytest_asyncio.fixture
async def store(tmp_path):
    s = SQLiteScoreStore(tmp_path / "test.db")
    yield s
    s.close()


def _mock_llm_response(should_reduce: bool, reason: str = "test reason"):
    """Build a mock LLMResponse for governance judgment."""
    from nthlayer_common.llm import LLMResponse

    response_json = json.dumps({"should_reduce": should_reduce, "reason": reason})
    return LLMResponse(text=response_json, model="test-model", provider="anthropic")


def _make_governance(store, model="test-model", threshold=0.5):
    tracker = StoreTrendTracker(store)
    return ErrorBudgetGovernance(
        store=store, tracker=tracker, threshold=threshold, model=model,
    )


def _make_score(eval_id: str, dims: dict[str, float]) -> QualityScore:
    return QualityScore(
        eval_id=eval_id,
        agent_name="agent-a",
        task_id="t1",
        dimensions=dims,
        confidence=0.8,
        evaluator_model="test",
    )


@pytest.mark.asyncio
async def test_no_scores_no_action(store):
    """No evaluations → no model call, no action."""
    governance = _make_governance(store)
    action = await governance.check_agent("agent-a")
    assert action is None


@pytest.mark.asyncio
async def test_no_model_no_action(store):
    """No model configured → no governance judgment (ZFC: fail open)."""
    governance = _make_governance(store, model=None)
    await store.save_score(_make_score("e1", {"correctness": 0.1}))

    action = await governance.check_agent("agent-a")
    assert action is None


@pytest.mark.asyncio
async def test_model_says_no_reduction(store):
    """Model decides good scores don't warrant reduction."""
    governance = _make_governance(store)
    await store.save_score(_make_score("e1", {"correctness": 0.9}))

    with patch("nthlayer_common.llm.llm_call",
               return_value=_mock_llm_response(False, "Scores are healthy")):
        action = await governance.check_agent("agent-a")

    assert action is None


@pytest.mark.asyncio
async def test_model_says_reduce(store):
    """Model decides low scores warrant autonomy reduction."""
    governance = _make_governance(store)
    await store.save_score(_make_score("e1", {"correctness": 0.3}))

    with patch("nthlayer_common.llm.llm_call",
               return_value=_mock_llm_response(True, "Correctness is critically low")):
        action = await governance.check_agent("agent-a")

    assert action is not None
    assert action.target_level == AutonomyLevel.SUPERVISED
    assert action.reason == "Correctness is critically low"


@pytest.mark.asyncio
async def test_model_failure_fails_open(store):
    """Model unavailability → no action (ZFC: fail open)."""
    governance = _make_governance(store)
    await store.save_score(_make_score("e1", {"correctness": 0.1}))

    with patch("nthlayer_common.llm.llm_call",
               side_effect=Exception("API down")):
        action = await governance.check_agent("agent-a")

    assert action is None


@pytest.mark.asyncio
async def test_already_suspended_no_call(store):
    """Already at lowest level → no model call needed."""
    governance = _make_governance(store)
    await store.save_score(_make_score("e1", {"correctness": 0.1}))
    await store.set_autonomy("agent-a", "suspended", "governance")

    # No mock needed — should return None before reaching model
    action = await governance.check_agent("agent-a")
    assert action is None


@pytest.mark.asyncio
async def test_governance_prompt_includes_context(store):
    """Governance prompt should include operator threshold as context, not trigger."""
    governance = _make_governance(store, threshold=0.6)
    await store.save_score(_make_score("e1", {"correctness": 0.5, "safety": 0.4}))

    mock_llm = MagicMock(return_value=_mock_llm_response(False, "Not yet"))

    with patch("nthlayer_common.llm.llm_call", mock_llm):
        await governance.check_agent("agent-a")

    call_args = mock_llm.call_args
    prompt = call_args.kwargs.get("user") or call_args[1].get("user") or call_args[0][1]
    assert "0.6" in prompt  # threshold as context
    assert "correctness" in prompt
    assert "safety" in prompt
    assert "agent-a" in prompt


@pytest.mark.asyncio
async def test_default_autonomy_is_full(store):
    governance = _make_governance(store)
    level = await governance.get_autonomy("unknown")
    assert level == AutonomyLevel.FULL


@pytest.mark.asyncio
async def test_safety_ratchet_requires_approver(store):
    governance = _make_governance(store)
    with pytest.raises(ValueError, match="approver"):
        await governance.restore_autonomy("agent-a", AutonomyLevel.FULL, "")


@pytest.mark.asyncio
async def test_restore_autonomy_with_approver(store):
    governance = _make_governance(store)
    await store.set_autonomy("agent-a", "supervised", "governance")
    await governance.restore_autonomy("agent-a", AutonomyLevel.FULL, "human-admin")
    level = await governance.get_autonomy("agent-a")
    assert level == AutonomyLevel.FULL


@pytest.mark.asyncio
async def test_reduction_ladder(store):
    assert ErrorBudgetGovernance._reduce_level(AutonomyLevel.FULL) == AutonomyLevel.SUPERVISED
    assert ErrorBudgetGovernance._reduce_level(AutonomyLevel.SUPERVISED) == AutonomyLevel.ADVISORY_ONLY
    assert ErrorBudgetGovernance._reduce_level(AutonomyLevel.ADVISORY_ONLY) == AutonomyLevel.SUSPENDED
    assert ErrorBudgetGovernance._reduce_level(AutonomyLevel.SUSPENDED) == AutonomyLevel.SUSPENDED


def test_parse_governance_response(store):
    governance = _make_governance(store)
    should, reason = governance.parse_governance_response(
        '{"should_reduce": true, "reason": "scores are bad"}'
    )
    assert should is True
    assert reason == "scores are bad"


def test_parse_governance_response_with_fences(store):
    governance = _make_governance(store)
    should, reason = governance.parse_governance_response(
        '```json\n{"should_reduce": false, "reason": "all good"}\n```'
    )
    assert should is False
    assert reason == "all good"
