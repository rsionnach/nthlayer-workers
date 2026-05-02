"""Tests for Arbiter data types."""

import pytest
from nthlayer_workers.measure.types import (
    AgentOutput,
    AutonomyLevel,
    GovernanceAction,
    QualityScore,
    TrendWindow,
)


def test_quality_score_construction():
    score = QualityScore(
        eval_id="e1",
        agent_name="agent-a",
        task_id="t1",
        dimensions={"correctness": 0.9, "style": 0.7},
        reasoning={"correctness": "Mostly correct", "style": "Needs work"},
        confidence=0.85,
        evaluator_model="test-model",
    )
    assert score.dimensions["correctness"] == 0.9
    assert score.reasoning["style"] == "Needs work"
    assert score.confidence == 0.85


def test_quality_score_reasoning_defaults_empty():
    score = QualityScore(
        eval_id="e2",
        agent_name="agent-a",
        task_id="t2",
        dimensions={"correctness": 0.5},
    )
    assert score.reasoning == {}
    assert score.confidence == 0.0
    assert score.evaluator_model == ""


def test_quality_score_frozen():
    score = QualityScore(
        eval_id="e3",
        agent_name="agent-a",
        task_id="t3",
        dimensions={"x": 1.0},
    )
    with pytest.raises(AttributeError):
        score.eval_id = "changed"  # type: ignore[misc]


def test_agent_output_construction():
    output = AgentOutput(
        agent_name="bot",
        task_id="t1",
        output_content="hello",
        output_type="text",
    )
    assert output.agent_name == "bot"
    assert output.metadata == {}


@pytest.mark.skip(
    reason=(
        "Asserts on the pre-P3-C.2 3-level enum (FULL/SUPERVISED/"
        "SUSPENDED). Current taxonomy is the 5-level ladder pinned "
        "by test_measure_worker.py::TestReduceAutonomy. opensrm-mnyj."
    )
)
def test_autonomy_level_values():
    assert AutonomyLevel.FULL.value == "full"
    assert AutonomyLevel.SUSPENDED.value == "suspended"


def test_trend_window_construction():
    tw = TrendWindow(
        agent_name="a",
        window_days=7,
        dimension_averages={"x": 0.5},
        evaluation_count=10,
        confidence_mean=0.8,
    )
    assert tw.evaluation_count == 10


def test_trend_window_new_fields_default():
    tw = TrendWindow(
        agent_name="a",
        window_days=7,
        dimension_averages={},
        evaluation_count=0,
        confidence_mean=0.0,
    )
    assert tw.reversal_rate == 0.0
    assert tw.total_cost_usd == 0.0
    assert tw.avg_cost_per_eval == 0.0


def test_trend_window_new_fields_explicit():
    tw = TrendWindow(
        agent_name="a",
        window_days=7,
        dimension_averages={"x": 0.9},
        evaluation_count=5,
        confidence_mean=0.85,
        reversal_rate=0.2,
        total_cost_usd=0.05,
        avg_cost_per_eval=0.01,
    )
    assert tw.reversal_rate == 0.2
    assert tw.total_cost_usd == 0.05
    assert tw.avg_cost_per_eval == 0.01


def test_quality_score_tier_field():
    score = QualityScore(
        eval_id="e1", agent_name="a", task_id="t1",
        dimensions={"correctness": 0.9},
        tier="standard", auto_approved=False,
    )
    assert score.tier == "standard"
    assert score.auto_approved is False


def test_quality_score_tier_defaults():
    score = QualityScore(
        eval_id="e1", agent_name="a", task_id="t1",
        dimensions={"correctness": 0.9},
    )
    assert score.tier is None
    assert score.auto_approved is False


@pytest.mark.skip(
    reason=(
        "References pre-P3-C.2 AutonomyLevel.SUPERVISED. The "
        "GovernanceAction shape is also legacy from the model-based "
        "governance engine; deterministic v1.5 governance lives in "
        "measure/worker.py and emits autonomy_change verdicts directly. "
        "opensrm-mnyj."
    )
)
def test_governance_action_construction():
    ga = GovernanceAction(
        agent_name="bot",
        target_level=AutonomyLevel.SUPERVISED,
        reason="Score dropped",
    )
    assert ga.target_level == AutonomyLevel.SUPERVISED
