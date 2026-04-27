"""Tests for Devin adapter — session parsing, structured output, dedup."""

import json

from nthlayer_workers.measure.adapters.devin import DevinAdapter


def _make_session(session_id: str = "s1", status: str = "completed", title: str = "Fix bug", structured_output: dict | None = None):
    return {
        "session_id": session_id,
        "status": status,
        "title": title,
        "structured_output": structured_output,
        "created_at": "2025-01-01T00:00:00Z",
    }


def test_parse_session_to_agent_output():
    """Completed session should be converted to AgentOutput."""
    session = _make_session()
    output = DevinAdapter._to_agent_output(session)

    assert output.agent_name == "devin:s1"
    assert output.task_id == "s1"
    assert output.output_type == "devin-session"
    assert output.output_content == "Fix bug"
    assert output.metadata["status"] == "completed"


def test_structured_output_used():
    """Structured output should be serialized as content when present."""
    structured = {"result": "success", "files_changed": 3}
    session = _make_session(structured_output=structured)
    output = DevinAdapter._to_agent_output(session)

    parsed = json.loads(output.output_content)
    assert parsed["result"] == "success"
    assert parsed["files_changed"] == 3


def test_skip_incomplete_sessions():
    """Only completed/stopped/failed sessions should be marked complete."""
    assert DevinAdapter._is_complete(_make_session(status="completed")) is True
    assert DevinAdapter._is_complete(_make_session(status="stopped")) is True
    assert DevinAdapter._is_complete(_make_session(status="failed")) is True
    assert DevinAdapter._is_complete(_make_session(status="running")) is False
    assert DevinAdapter._is_complete(_make_session(status="queued")) is False


def test_dedup_seen_sessions():
    """Adapter should track seen session IDs."""
    adapter = DevinAdapter(api_key="test-key")
    assert "s1" not in adapter._seen

    adapter._seen.add("s1")
    assert "s1" in adapter._seen


def test_adapter_name():
    adapter = DevinAdapter(api_key="test-key")
    assert adapter.name() == "devin"


def test_session_without_structured_output():
    """Session without structured_output should fall back to title."""
    session = _make_session(title="Deploy service", structured_output=None)
    output = DevinAdapter._to_agent_output(session)
    assert output.output_content == "Deploy service"
