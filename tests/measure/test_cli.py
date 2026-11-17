"""Tests for CLI subcommands."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from nthlayer_workers.measure.cli import (
    cmd_calibrate,
    cmd_evaluate,
    cmd_overrides_list,
    cmd_status,
    main,
)
from nthlayer_workers.measure.types import QualityScore, TrendWindow


@pytest.fixture
def config_file(tmp_path):
    cfg = tmp_path / "arbiter.yaml"
    cfg.write_text(
        "evaluator:\n"
        "  model: test-model\n"
        "store:\n"
        "  backend: sqlite\n"
        "  path: " + str(tmp_path / "test.db") + "\n"
        "dimensions:\n"
        "  - correctness\n"
        "  - style\n"
        "agents:\n"
        "  - name: test-agent\n"
        "    adapter: webhook\n"
    )
    return cfg


def _make_args(**kwargs):
    import argparse

    ns = argparse.Namespace()
    for k, v in kwargs.items():
        setattr(ns, k, v)
    return ns


def test_main_shows_help(capsys):
    with pytest.raises(SystemExit) as exc_info:
        import sys

        with patch.object(sys, "argv", ["arbiter", "--help"]):
            main()
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    # Live subcommands after opensrm-t5yr (serve/api-serve/governance removed).
    assert "evaluate" in captured.out
    assert "status" in captured.out
    assert "calibrate" in captured.out
    assert "overrides" in captured.out
    assert "tiering" in captured.out


def test_evaluate_reads_file(tmp_path, config_file, capsys):
    test_file = tmp_path / "input.txt"
    test_file.write_text("test output content")

    score = QualityScore(
        eval_id="test-eval",
        agent_name="test-agent",
        task_id="cli-eval",
        dimensions={"correctness": 0.9, "style": 0.8},
        confidence=0.85,
        evaluator_model="test-model",
        cost_usd=0.01,
    )

    mock_evaluator = AsyncMock()
    mock_evaluator.evaluate = AsyncMock(return_value=score)

    mock_store = AsyncMock()
    mock_store.save_score = AsyncMock()

    with patch("nthlayer_workers.measure.cli._build_evaluator", return_value=mock_evaluator), \
         patch("nthlayer_workers.measure.cli._build_store", return_value=mock_store):
        args = _make_args(
            config=config_file,
            file=test_file,
            agent_name="test-agent",
            task_id="cli-eval",
            output_type="text",
        )
        cmd_evaluate(args)

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["eval_id"] == "test-eval"
    assert result["dimensions"]["correctness"] == 0.9


def test_status_shows_window(config_file, capsys):
    window = TrendWindow(
        agent_name="test-agent",
        window_days=7,
        dimension_averages={"correctness": 0.85},
        evaluation_count=10,
        confidence_mean=0.9,
        reversal_rate=0.1,
        total_cost_usd=0.50,
        avg_cost_per_eval=0.05,
    )

    mock_store = AsyncMock()
    mock_store.get_autonomy = AsyncMock(return_value="full")

    mock_tracker = AsyncMock()
    mock_tracker.compute_window = AsyncMock(return_value=window)

    with patch("nthlayer_workers.measure.cli._build_store", return_value=mock_store), \
         patch("nthlayer_workers.measure.cli._build_tracker", return_value=mock_tracker):
        args = _make_args(
            config=config_file,
            agent_name="test-agent",
            window_days=7,
        )
        cmd_status(args)

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["agent_name"] == "test-agent"
    assert result["reversal_rate"] == 0.1
    assert result["autonomy"] == "full"


def test_calibrate_runs_report(config_file, capsys):
    from nthlayer_workers.measure.calibration.loop import CalibrationReport

    report = CalibrationReport(
        total_overrides=5,
        mean_absolute_error=0.15,
        dimensions_analyzed=["correctness", "style"],
    )

    mock_store = AsyncMock()
    mock_cal = AsyncMock()
    mock_cal.calibrate = AsyncMock(return_value=report)

    with patch("nthlayer_workers.measure.cli._build_store", return_value=mock_store), \
         patch("nthlayer_workers.measure.calibration.loop.OverrideCalibration", return_value=mock_cal):
        args = _make_args(config=config_file, window_days=30, agent=None)
        cmd_calibrate(args)

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["total_overrides"] == 5
    assert result["mean_absolute_error"] == pytest.approx(0.15)


def test_overrides_list(config_file, capsys):
    mock_store = AsyncMock()
    mock_store.get_overrides = AsyncMock(return_value=[
        {"eval_id": "e1", "dimension": "correctness", "original_score": 0.9, "corrected_score": 0.5}
    ])

    with patch("nthlayer_workers.measure.cli._build_store", return_value=mock_store):
        args = _make_args(config=config_file, days=7, agent=None)
        cmd_overrides_list(args)

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert len(result) == 1
    assert result[0]["eval_id"] == "e1"




# ------------------------------------------------------------------ #
# calibrate --agent reads the agent's manifest through nthlayer-common  #
# [opensrm-n4v1]                                                       #
# ------------------------------------------------------------------ #

# The shape of the shipped nthlayer/demo/specs/fraud-detect.yaml (srm/v1).
_SRM_V1_MANIFEST = (
    "apiVersion: srm/v1\n"
    "kind: ServiceReliabilityManifest\n"
    "metadata:\n"
    "  name: fraud-detect\n"
    "  team: payments-ml\n"
    "  tier: critical\n"
    "spec:\n"
    "  type: ai-gate\n"
    "  slos:\n"
    "    reversal_rate:\n"
    "      target: 98.5\n"
    "      window: 30d\n"
)

# The loader's old, bespoke shape under the published opensrm/v1 apiVersion.
_BESPOKE_MANIFEST = (
    "apiVersion: opensrm/v1\n"
    "kind: ServiceReliabilityManifest\n"
    "metadata:\n"
    "  name: fraud-detect\n"
    "spec:\n"
    "  type: ai-gate\n"
    "  slos:\n"
    "    judgment:\n"
    "      reversal:\n"
    "        rate:\n"
    "          target: 0.05\n"
)


def _agent_config(tmp_path, manifest_path=None):
    cfg = tmp_path / "measure.yaml"
    cfg.write_text(
        "evaluator:\n"
        "  model: test-model\n"
        "store:\n"
        "  backend: sqlite\n"
        "  path: " + str(tmp_path / "scores.db") + "\n"
        "agents:\n"
        "  - name: fraud-detect\n"
        "    adapter: webhook\n"
        + (f"    manifest: {manifest_path}\n" if manifest_path is not None else "")
    )
    return cfg


def test_calibrate_agent_reports_the_manifests_reversal_ceiling(tmp_path, capsys):
    manifest = tmp_path / "fraud-detect.yaml"
    manifest.write_text(_SRM_V1_MANIFEST)
    args = _make_args(
        config=_agent_config(tmp_path, manifest), window_days=30, agent="fraud-detect"
    )

    cmd_calibrate(args)

    result = json.loads(capsys.readouterr().out)
    assert result["agent_name"] == "fraud-detect"
    assert result["reversal_rate_target"] == 0.015
    assert result["reversal_rate_compliant"] is True
    # No manifest format declares a quality threshold.
    assert result["false_accept_rate"] is None
    assert result["precision"] is None
    assert result["recall"] is None


def test_calibrate_agent_without_a_manifest_reports_no_target(tmp_path, capsys):
    args = _make_args(config=_agent_config(tmp_path), window_days=30, agent="fraud-detect")

    cmd_calibrate(args)

    result = json.loads(capsys.readouterr().out)
    assert result["reversal_rate_target"] is None
    assert result["reversal_rate_compliant"] is None


def test_calibrate_agent_fails_on_a_rejected_manifest(tmp_path, capsys):
    """A configured manifest the loader rejects is an error, not a report
    without a target."""
    manifest = tmp_path / "old.yaml"
    manifest.write_text(_BESPOKE_MANIFEST)
    args = _make_args(
        config=_agent_config(tmp_path, manifest), window_days=30, agent="fraud-detect"
    )

    with pytest.raises(SystemExit) as exc_info:
        cmd_calibrate(args)

    assert exc_info.value.code == 1
    assert capsys.readouterr().out == ""
    assert not (tmp_path / "scores.db").exists()


def test_calibrate_agent_fails_on_a_missing_manifest(tmp_path, capsys):
    args = _make_args(
        config=_agent_config(tmp_path, tmp_path / "nowhere.yaml"),
        window_days=30,
        agent="fraud-detect",
    )

    with pytest.raises(SystemExit) as exc_info:
        cmd_calibrate(args)

    assert exc_info.value.code == 1
    assert capsys.readouterr().out == ""
