# tests/test_replay.py
"""Functional replay tests — the primary acceptance criterion for Phase 3."""
from __future__ import annotations

import os


from nthlayer_workers.respond.cli import replay_command


# All replay tests use --no-model mode (mock responses from YAML).


async def test_replay_cascading_failure_no_model(tmp_path):
    """The primary acceptance criterion: full pipeline from SitRep trigger to RESOLVED."""
    scenario_path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "scenarios",
        "synthetic",
        "cascading-failure.yaml",
    )
    result = await replay_command(
        scenario_path, config_path=None, no_model=True, work_dir=str(tmp_path)
    )
    assert result["final_state"] == "resolved"
    assert result["verdict_count"] >= 5
    assert len(result["verdict_chain"]) >= 5


async def test_replay_pagerduty_trigger(tmp_path):
    """PagerDuty-triggered scenario (no SitRep) runs to RESOLVED."""
    scenario_path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "scenarios",
        "synthetic",
        "sitrep-unavailable.yaml",
    )
    result = await replay_command(
        scenario_path, config_path=None, no_model=True, work_dir=str(tmp_path)
    )
    assert result["final_state"] == "resolved"


async def test_replay_model_unavailable(tmp_path):
    """All mock_responses null -> agents degrade -> ESCALATED."""
    scenario_path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "scenarios",
        "synthetic",
        "model-unavailable.yaml",
    )
    result = await replay_command(
        scenario_path, config_path=None, no_model=True, work_dir=str(tmp_path)
    )
    assert result["final_state"] == "escalated"


async def test_replay_remediation_approval(tmp_path):
    """Remediation requires approval -> interaction approves -> RESOLVED."""
    scenario_path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "scenarios",
        "synthetic",
        "remediation-approval.yaml",
    )
    result = await replay_command(
        scenario_path, config_path=None, no_model=True, work_dir=str(tmp_path)
    )
    assert result["final_state"] == "resolved"
    assert result["remediation_executed"] is True


async def test_replay_autonomy_reduction(tmp_path):
    """Agent model update triggers autonomy reduction -> RESOLVED."""
    scenario_path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "scenarios",
        "synthetic",
        "autonomy-reduction.yaml",
    )
    result = await replay_command(
        scenario_path, config_path=None, no_model=True, work_dir=str(tmp_path)
    )
    assert result["final_state"] == "resolved"
    assert result["verdict_count"] >= 5
    assert result["remediation_executed"] is True


async def test_replay_crash_recovery(tmp_path):
    """Pipeline completes; context persisted and loadable for crash recovery."""
    scenario_path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "scenarios",
        "synthetic",
        "crash-recovery.yaml",
    )
    result = await replay_command(
        scenario_path, config_path=None, no_model=True, work_dir=str(tmp_path)
    )
    assert result["final_state"] == "resolved"
    assert result["verdict_count"] >= 5
    assert result["remediation_executed"] is True


async def test_replay_low_confidence_escalation(tmp_path):
    """Investigation too uncertain -> pipeline escalates."""
    scenario_path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "scenarios",
        "synthetic",
        "low-confidence-escalation.yaml",
    )
    result = await replay_command(
        scenario_path, config_path=None, no_model=True, work_dir=str(tmp_path)
    )
    assert result["final_state"] == "escalated"
    assert result["remediation_executed"] is False


async def test_replay_human_override(tmp_path):
    """Human rejects triage -> incident escalated."""
    scenario_path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "scenarios",
        "synthetic",
        "human-override.yaml",
    )
    result = await replay_command(
        scenario_path, config_path=None, no_model=True, work_dir=str(tmp_path)
    )
    assert result["final_state"] == "escalated"
