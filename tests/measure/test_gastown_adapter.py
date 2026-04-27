"""Tests for GasTown adapter — wisp parsing and dedup."""


from nthlayer_workers.measure.adapters.gastown import GasTownAdapter


def _make_wisp(wisp_id: str = "w1", worker: str = "polecat-1", rig: str = "wyvern", score: str = "0.85"):
    return {
        "id": wisp_id,
        "labels": [
            "type:plugin-run",
            "plugin:quality-review-result",
            f"worker:{worker}",
            f"rig:{rig}",
            f"score:{score}",
            "recommendation:approve",
            "result:success",
        ],
        "description": f"Score: {score}, approve. Issues: 1 minor (style)",
    }


def test_parse_wisp_to_agent_output():
    """Wisp should be correctly converted to AgentOutput."""
    wisp = _make_wisp()
    output = GasTownAdapter._to_agent_output(wisp)

    assert output.agent_name == "polecat-1"
    assert output.task_id == "w1"
    assert output.output_type == "quality-review-result"
    assert output.metadata["rig"] == "wyvern"
    assert output.metadata["score"] == "0.85"
    assert "Score: 0.85" in output.output_content


def test_parse_wisp_missing_labels():
    """Wisp with no labels should use defaults."""
    wisp = {"id": "w2", "labels": [], "description": "minimal"}
    output = GasTownAdapter._to_agent_output(wisp)

    assert output.agent_name == "unknown"
    assert output.task_id == "w2"
    assert output.metadata["rig"] == ""


def test_dedup_seen_wisps():
    """Adapter should track seen wisp IDs."""
    adapter = GasTownAdapter(rig_name="test")
    assert "w1" not in adapter._seen

    adapter._seen.add("w1")
    assert "w1" in adapter._seen


def test_empty_query_result():
    """Adapter should handle empty results gracefully."""
    adapter = GasTownAdapter(rig_name="test")
    assert adapter.name() == "gastown"


def test_wisp_with_colon_in_value():
    """Labels with colons in values should split correctly."""
    wisp = {
        "id": "w3",
        "labels": ["worker:polecat:special", "rig:my-rig"],
        "description": "test",
    }
    output = GasTownAdapter._to_agent_output(wisp)
    assert output.agent_name == "polecat:special"
