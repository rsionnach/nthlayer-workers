"""Tests for API input normalisation."""
import pytest

from nthlayer_workers.measure.api.normalise import EvaluationRequest, normalise_input


def test_all_fields_populated():
    body = {
        "agent": "code-reviewer",
        "task_id": "PR-1234",
        "output": "Looks good.",
        "context": "Review PR #1234",
        "service": "webapp",
        "environment": "staging",
        "callback_url": "https://example.com/callback",
        "metadata": {"priority": "high"},
    }
    req = normalise_input(body)
    assert req.agent_name == "code-reviewer"
    assert req.task_id == "PR-1234"
    assert req.output == "Looks good."
    assert req.context == "Review PR #1234"
    assert req.service == "webapp"
    assert req.environment == "staging"
    assert req.callback_url == "https://example.com/callback"
    assert req.metadata == {"priority": "high"}


def test_minimal_input_fills_defaults():
    body = {"agent": "test-agent", "output": "hello world"}
    req = normalise_input(body)
    assert req.agent_name == "test-agent"
    assert req.output == "hello world"
    assert len(req.task_id) > 0  # UUID generated
    assert req.environment == "production"
    assert req.context is None
    assert req.service is None
    assert req.callback_url is None
    assert req.metadata == {}


def test_missing_agent_raises():
    with pytest.raises(ValueError, match="agent"):
        normalise_input({"output": "hello"})


def test_missing_output_raises():
    with pytest.raises(ValueError, match="output"):
        normalise_input({"agent": "test"})


def test_extra_fields_ignored():
    body = {"agent": "test", "output": "hi", "unknown_field": 42, "extra": True}
    req = normalise_input(body)
    assert req.agent_name == "test"
    assert req.output == "hi"


def test_returns_evaluation_request_type():
    req = normalise_input({"agent": "a", "output": "b"})
    assert isinstance(req, EvaluationRequest)
