"""Tests for SitRep CLI."""
from __future__ import annotations

import os

import pytest

from nthlayer_workers.correlate.cli import parse_relative_time, replay_command, status_command


class TestParseRelativeTime:
    def test_zero_minutes(self):
        result = parse_relative_time("T+0m")
        assert "2026-01-01T00:00:00" in result

    def test_twelve_minutes(self):
        result = parse_relative_time("T+12m")
        assert "2026-01-01T00:12:00" in result

    def test_invalid_format(self):
        with pytest.raises(ValueError):
            parse_relative_time("invalid")


class TestReplayCommand:
    def test_replay_cascading_failure_no_model(self, tmp_path):
        scenario_path = os.path.join(
            os.path.dirname(__file__), "..", "scenarios", "synthetic", "cascading-failure.yaml"
        )
        result = replay_command(
            scenario_path=scenario_path,
            config_path=None,
            no_model=True,
            store_dir=str(tmp_path),
        )
        assert result == 0  # success

    def test_replay_quiet_period_no_model(self, tmp_path):
        scenario_path = os.path.join(
            os.path.dirname(__file__), "..", "scenarios", "synthetic", "quiet-period.yaml"
        )
        result = replay_command(
            scenario_path=scenario_path,
            config_path=None,
            no_model=True,
            store_dir=str(tmp_path),
        )
        assert result == 0

    def test_replay_nonexistent_scenario(self, tmp_path):
        result = replay_command(
            scenario_path="/nonexistent/path.yaml",
            config_path=None,
            no_model=True,
            store_dir=str(tmp_path),
        )
        assert result == 2  # error

    def test_replay_misleading_correlation_no_model(self, tmp_path):
        scenario_path = os.path.join(
            os.path.dirname(__file__), "..", "scenarios", "synthetic", "misleading-correlation.yaml"
        )
        result = replay_command(
            scenario_path=scenario_path,
            config_path=None,
            no_model=True,
            store_dir=str(tmp_path),
        )
        assert result == 0


class TestStatusCommand:
    def test_status_default(self, tmp_path):
        result = status_command(config_path=None, store_dir=str(tmp_path))
        assert result == 0


# ---------------------------------------------------------------------------
# Task 7: Config + CLI flags for trace backend
# ---------------------------------------------------------------------------

from nthlayer_workers.correlate.config import load_config


class TestConfigTraces:
    def test_traces_section_parsed(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""\
traces:
  backend: tempo
  detail: summary
  baseline_window: 2h
  tempo:
    endpoint: "http://tempo:3200"
    org_id: "my-tenant"
    timeout_seconds: 60
    use_service_graphs: false
""")
        cfg = load_config(str(config_file))
        assert cfg.trace_backend == "tempo"
        assert cfg.trace_detail == "summary"
        assert cfg.trace_baseline_window == "2h"
        assert cfg.tempo_endpoint == "http://tempo:3200"
        assert cfg.tempo_org_id == "my-tenant"
        assert cfg.tempo_timeout == 60
        assert cfg.tempo_use_service_graphs is False

    def test_defaults_without_traces_section(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("store:\n  path: events.db\n")
        cfg = load_config(str(config_file))
        assert cfg.trace_backend is None
        assert cfg.trace_detail == "full"
        assert cfg.tempo_endpoint == "http://localhost:3200"
        assert cfg.tempo_use_service_graphs is True

    def test_tempo_subsection(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""\
traces:
  tempo:
    endpoint: "http://custom:3200"
""")
        cfg = load_config(str(config_file))
        assert cfg.tempo_endpoint == "http://custom:3200"


class TestCLITraceFlags:
    def test_correlate_parser_accepts_trace_flags(self):
        from nthlayer_workers.correlate.cli import _build_parser

        parser = _build_parser()
        args = parser.parse_args([
            "correlate",
            "--trigger-verdict", "vrd-test",
            "--prometheus-url", "http://prom:9090",
            "--specs-dir", "/specs",
            "--trace-backend", "tempo",
            "--tempo-endpoint", "http://tempo:3200",
            "--trace-detail", "summary",
        ])
        assert args.trace_backend == "tempo"
        assert args.tempo_endpoint == "http://tempo:3200"
        assert args.trace_detail == "summary"

    def test_correlate_parser_trace_flags_optional(self):
        from nthlayer_workers.correlate.cli import _build_parser
        parser = _build_parser()
        args = parser.parse_args([
            "correlate",
            "--trigger-verdict", "vrd-test",
            "--prometheus-url", "http://prom:9090",
            "--specs-dir", "/specs",
        ])
        assert args.trace_backend is None
        assert args.tempo_endpoint is None
        assert args.trace_detail == "full"
