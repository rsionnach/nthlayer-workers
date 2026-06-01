"""Tests for CLI subcommands."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from nthlayer_workers.measure.cli import (
    cmd_calibrate,
    cmd_evaluate,
    cmd_governance_show,
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
    assert "serve" in captured.out
    assert "evaluate" in captured.out
    assert "status" in captured.out
    assert "calibrate" in captured.out
    assert "overrides" in captured.out
    assert "governance" in captured.out


def test_serve_subcommand_listed(capsys):
    with pytest.raises(SystemExit):
        import sys

        with patch.object(sys, "argv", ["arbiter", "serve", "--help"]):
            main()
    captured = capsys.readouterr()
    assert "evaluation pipeline" in captured.out.lower() or "serve" in captured.out.lower()


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


def test_governance_show(config_file, capsys):
    mock_store = AsyncMock()
    mock_store.get_autonomy = AsyncMock(return_value="supervised")

    with patch("nthlayer_workers.measure.cli._build_store", return_value=mock_store):
        args = _make_args(config=config_file, agent_name="test-agent")
        cmd_governance_show(args)

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["autonomy"] == "supervised"


