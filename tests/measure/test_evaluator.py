"""Tests for ModelEvaluator — prompt construction + structured evaluation."""

from unittest.mock import patch

import pytest
from pydantic import ValidationError

from nthlayer_workers.measure.pipeline.evaluator import (
    DimensionScore,
    EvaluationResult,
    ModelEvaluator,
    _compute_cost,
    _to_quality_score,
)
from nthlayer_workers.measure.types import AgentOutput


@pytest.fixture
def evaluator():
    return ModelEvaluator(model="test-model", max_tokens=2048, timeout=30.0)


@pytest.fixture
def sample_output():
    return AgentOutput(
        agent_name="agent-a",
        task_id="task-1",
        output_content="def hello(): return 'world'",
        output_type="code",
    )


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class TestPydanticModels:
    def test_evaluation_result_valid(self):
        result = EvaluationResult(
            dimensions={"correctness": DimensionScore(score=0.9, reasoning="Good")},
            confidence=0.85,
        )
        assert result.dimensions["correctness"].score == 0.9
        assert result.confidence == 0.85

    def test_dimension_score_default_reasoning(self):
        ds = DimensionScore(score=0.5)
        assert ds.reasoning == ""

    def test_out_of_range_rejected(self):
        with pytest.raises(ValidationError):
            DimensionScore(score=1.5)

    def test_negative_score_rejected(self):
        with pytest.raises(ValidationError):
            DimensionScore(score=-0.1)


# ---------------------------------------------------------------------------
# Prompt construction (unchanged)
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    def test_contains_dimensions(self, evaluator, sample_output):
        prompt = evaluator.build_prompt(sample_output, ["correctness", "style"])
        assert "correctness" in prompt
        assert "style" in prompt
        assert "agent-a" in prompt
        assert "task-1" in prompt
        assert "def hello()" in prompt

    def test_contains_response_format(self, evaluator, sample_output):
        prompt = evaluator.build_prompt(sample_output, ["correctness"])
        assert '"dimensions"' in prompt
        assert '"confidence"' in prompt
        assert "JSON" in prompt


# ---------------------------------------------------------------------------
# Cost computation
# ---------------------------------------------------------------------------


class TestComputeCost:
    def test_known_model(self):
        cost = _compute_cost("claude-sonnet-4-20250514", 1000, 500)
        assert cost == pytest.approx(0.0105)

    def test_unknown_model(self):
        assert _compute_cost("unknown-model", 1000, 500) is None


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------


class TestToQualityScore:
    def test_converts_correctly(self, sample_output):
        result = EvaluationResult(
            dimensions={
                "correctness": DimensionScore(score=0.9, reasoning="Good"),
                "style": DimensionScore(score=0.7),
            },
            confidence=0.85,
        )
        score = _to_quality_score(result, sample_output, "test-model")
        assert score.agent_name == "agent-a"
        assert score.task_id == "task-1"
        assert score.dimensions["correctness"] == pytest.approx(0.9)
        assert score.dimensions["style"] == pytest.approx(0.7)
        assert score.reasoning["correctness"] == "Good"
        assert "style" not in score.reasoning  # empty reasoning filtered
        assert score.confidence == pytest.approx(0.85)
        assert score.evaluator_model == "test-model"


# ---------------------------------------------------------------------------
# Evaluator integration
# ---------------------------------------------------------------------------


class TestEvaluate:
    @patch("nthlayer_workers.measure.pipeline.evaluator.load_prompt")
    @patch("nthlayer_workers.measure.pipeline.evaluator.structured_call_with_usage")
    async def test_evaluate_returns_quality_score(self, mock_call, mock_prompt, evaluator, sample_output):
        from unittest.mock import MagicMock

        from nthlayer_common.llm_structured import StructuredCallResult, StructuredCallUsage

        mock_spec = MagicMock()
        mock_spec.system = "evaluate this"
        mock_spec.user_template = "{agent_name} {task_id} {output_type} {output_content} {dimensions_block}"
        mock_prompt.return_value = mock_spec

        mock_call.return_value = StructuredCallResult(
            data=EvaluationResult(
                dimensions={"correctness": DimensionScore(score=0.9, reasoning="Good")},
                confidence=0.85,
            ),
            usage=StructuredCallUsage(input_tokens=100, output_tokens=50),
        )

        score = await evaluator.evaluate(sample_output, ["correctness"])
        assert score.agent_name == "agent-a"
        assert score.dimensions["correctness"] == pytest.approx(0.9)
        assert score.confidence == pytest.approx(0.85)

    @patch("nthlayer_workers.measure.pipeline.evaluator.load_prompt")
    @patch("nthlayer_workers.measure.pipeline.evaluator.structured_call_with_usage")
    async def test_evaluate_preserves_cost(self, mock_call, mock_prompt, sample_output):
        from unittest.mock import MagicMock

        from nthlayer_common.llm_structured import StructuredCallResult, StructuredCallUsage

        mock_spec = MagicMock()
        mock_spec.system = "eval"
        mock_spec.user_template = "{agent_name} {task_id} {output_type} {output_content} {dimensions_block}"
        mock_prompt.return_value = mock_spec

        evaluator = ModelEvaluator(model="claude-sonnet-4-20250514")
        mock_call.return_value = StructuredCallResult(
            data=EvaluationResult(
                dimensions={"x": DimensionScore(score=0.5)},
                confidence=0.6,
            ),
            usage=StructuredCallUsage(input_tokens=1000, output_tokens=500),
        )

        score = await evaluator.evaluate(sample_output, ["x"])
        assert score.cost_usd is not None
        assert score.cost_usd == pytest.approx(0.0105)

    @patch("nthlayer_workers.measure.pipeline.evaluator.load_prompt")
    @patch("nthlayer_workers.measure.pipeline.evaluator.structured_call_with_usage")
    async def test_evaluate_with_model_override(self, mock_call, mock_prompt, evaluator, sample_output):
        from unittest.mock import MagicMock

        from nthlayer_common.llm_structured import StructuredCallResult, StructuredCallUsage

        mock_spec = MagicMock()
        mock_spec.system = "eval"
        mock_spec.user_template = "{agent_name} {task_id} {output_type} {output_content} {dimensions_block}"
        mock_prompt.return_value = mock_spec

        mock_call.return_value = StructuredCallResult(
            data=EvaluationResult(
                dimensions={"x": DimensionScore(score=0.5)},
                confidence=0.6,
            ),
            usage=StructuredCallUsage(input_tokens=100, output_tokens=50),
        )

        score = await evaluator.evaluate(sample_output, ["x"], model="anthropic/claude-haiku-4-20250414")
        assert score.evaluator_model == "anthropic/claude-haiku-4-20250414"
