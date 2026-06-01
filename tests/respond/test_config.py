# tests/test_config.py
"""Tests for nthlayer-respond configuration."""
import textwrap

from nthlayer_workers.respond.config import RespondConfig, load_config


def test_default_config():
    cfg = RespondConfig()
    assert cfg.poll_interval_seconds == 30
    assert cfg.escalation_threshold == 0.3
    assert cfg.model == "claude-sonnet-4-20250514"
    assert cfg.triage_timeout == 15
    assert cfg.investigation_timeout == 60
    assert cfg.communication_timeout == 20
    assert cfg.remediation_timeout == 30
    assert cfg.root_cause_threshold == 0.7
    assert cfg.cooldown_seconds == 300
    assert cfg.arbiter_url == "http://localhost:8080"
    assert cfg.verdict_store_path == "verdicts.db"
    assert cfg.context_store_path == "respond-incidents.db"


def test_load_config_from_yaml(tmp_path):
    config_file = tmp_path / "mayday.yaml"
    config_file.write_text(textwrap.dedent("""\
        coordinator:
          poll_interval_seconds: 10
          escalation_threshold: 0.5
        agents:
          model: claude-opus-4-20250514
          triage:
            timeout: 10
          investigation:
            timeout: 45
            root_cause_threshold: 0.8
        safe_actions:
          arbiter_url: http://arbiter:9090
        verdict:
          store:
            path: /tmp/verdicts.db
        context_store:
          path: /tmp/incidents.db
    """))
    cfg = load_config(str(config_file))
    assert cfg.poll_interval_seconds == 10
    assert cfg.escalation_threshold == 0.5
    assert cfg.model == "claude-opus-4-20250514"
    assert cfg.triage_timeout == 10
    assert cfg.investigation_timeout == 45
    assert cfg.root_cause_threshold == 0.8
    assert cfg.arbiter_url == "http://arbiter:9090"
    assert cfg.verdict_store_path == "/tmp/verdicts.db"
    assert cfg.context_store_path == "/tmp/incidents.db"


def test_load_config_missing_file():
    cfg = load_config("/nonexistent/path.yaml")
    assert cfg.model == "claude-sonnet-4-20250514"  # defaults


def test_load_config_partial_yaml(tmp_path):
    config_file = tmp_path / "mayday.yaml"
    config_file.write_text("coordinator:\n  poll_interval_seconds: 5\n")
    cfg = load_config(str(config_file))
    assert cfg.poll_interval_seconds == 5
    assert cfg.model == "claude-sonnet-4-20250514"  # default preserved


def test_server_config_defaults():
    """RespondConfig has server/approval/slack defaults."""
    config = RespondConfig()
    assert config.server_host == "0.0.0.0"
    assert config.server_port == 8090
    assert config.approval_timeout_seconds == 900
    assert config.slack_signing_secret == ""
    assert config.slack_bot_token == ""


def test_worker_mode_defaults():
    """RespondConfig exposes worker-mode (P3-E.1) defaults."""
    cfg = RespondConfig()
    assert cfg.cycle_interval_seconds == 30.0
    assert cfg.fallback_threshold_seconds == 60.0
    assert cfg.terminal_retention_seconds == 86400.0
    assert cfg.step_timeout_seconds == 90.0


def test_worker_mode_field_types():
    """Worker-mode fields are floats (suitable for asyncio.wait_for)."""
    cfg = RespondConfig()
    for name in (
        "cycle_interval_seconds",
        "fallback_threshold_seconds",
        "terminal_retention_seconds",
        "step_timeout_seconds",
    ):
        assert isinstance(getattr(cfg, name), float), f"{name} must be float"


def test_worker_mode_rejects_negative_fallback_threshold():
    """R5 edge-case fix: negative fallback_threshold_seconds would invert the
    cutoff (matching every breach including those that may yet receive a
    snapshot). __post_init__ raises rather than silently misbehaving."""
    import pytest
    with pytest.raises(ValueError, match="fallback_threshold_seconds must be >= 0"):
        RespondConfig(fallback_threshold_seconds=-1.0)


def test_worker_mode_rejects_negative_step_timeout():
    import pytest
    with pytest.raises(ValueError, match="step_timeout_seconds must be >= 0"):
        RespondConfig(step_timeout_seconds=-1.0)


def test_worker_mode_rejects_negative_cycle_interval():
    import pytest
    with pytest.raises(ValueError, match="cycle_interval_seconds must be >= 0"):
        RespondConfig(cycle_interval_seconds=-1.0)


def test_worker_mode_rejects_negative_terminal_retention():
    import pytest
    with pytest.raises(ValueError, match="terminal_retention_seconds must be >= 0"):
        RespondConfig(terminal_retention_seconds=-1.0)


def test_load_config_server_section(tmp_path):
    """load_config reads server, approval, and slack sections."""
    cfg_path = tmp_path / "respond.yaml"
    cfg_path.write_text("""
server:
  host: "127.0.0.1"
  port: 9090
approval:
  timeout_seconds: 600
slack:
  signing_secret: "test-secret"
  bot_token: "xoxb-test-token"
""")
    config = load_config(str(cfg_path))
    assert config.server_host == "127.0.0.1"
    assert config.server_port == 9090
    assert config.approval_timeout_seconds == 600
    assert config.slack_signing_secret == "test-secret"
    assert config.slack_bot_token == "xoxb-test-token"
