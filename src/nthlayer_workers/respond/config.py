# src/nthlayer_respond/config.py
"""nthlayer-respond configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass

import structlog
import yaml

logger = structlog.get_logger()


@dataclass
class RespondConfig:
    # Coordinator
    poll_interval_seconds: int = 30
    escalation_threshold: float = 0.3
    # Agents — NTHLAYER_MODEL env var takes precedence over hardcoded default
    model: str = os.environ.get("NTHLAYER_MODEL", "claude-sonnet-4-20250514")
    max_tokens: int = 4096
    triage_timeout: int = 15
    investigation_timeout: int = 60
    communication_timeout: int = 20
    remediation_timeout: int = 30
    root_cause_threshold: float = 0.7
    # Safe actions
    cooldown_seconds: int = 300
    arbiter_url: str = "http://localhost:8080"
    # Stores
    verdict_store_path: str = "verdicts.db"
    context_store_path: str = "respond-incidents.db"
    # Topology
    manifests_dir: str | None = None
    # Server
    server_host: str = "0.0.0.0"
    server_port: int = 8090
    # Approval
    approval_timeout_seconds: int = 900
    # Slack (interactive buttons)
    slack_signing_secret: str = ""
    slack_bot_token: str = ""
    # Notification backends (on-call escalation)
    ntfy_server_url: str = ""
    ntfy_auth_token: str = ""
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""
    pagerduty_routing_key: str = ""
    webhook_base_url: str = "http://localhost:8090"
    # Worker mode (P3-E.1) — set by nthlayer-workers CLI from nthlayer.yaml,
    # not from respond.yaml. Defaults exist so legacy CLI construction is
    # unaffected.
    cycle_interval_seconds: float = 30.0
    fallback_threshold_seconds: float = 60.0
    terminal_retention_seconds: float = 86400.0
    step_timeout_seconds: float = 90.0

    def __post_init__(self) -> None:
        # Validate worker-mode timing fields. Negative values silently invert
        # cutoff/threshold semantics (e.g. negative fallback_threshold makes
        # the cutoff a future timestamp, matching every breach including those
        # that may yet receive a snapshot). 0 is allowed across the board:
        # threshold=0 fires fallback immediately (used by integration tests),
        # retention=0 prunes terminal incidents on the next cycle, cycle=0 is
        # a busy loop (degenerate but not catastrophic), step_timeout=0 falls
        # through coordinator._step_timeout()'s `> 0` guard and disables the
        # per-step timeout. Validate at construction so misconfiguration of
        # the more dangerous case (negative) fails loud.
        for name in (
            "cycle_interval_seconds",
            "fallback_threshold_seconds",
            "terminal_retention_seconds",
            "step_timeout_seconds",
        ):
            value = getattr(self, name)
            if value < 0:
                raise ValueError(f"RespondConfig.{name} must be >= 0, got {value!r}")


def load_config(path: str) -> RespondConfig:
    """Load config from YAML file. Returns defaults if file missing."""
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.info("config_not_found", path=path)
        return RespondConfig()

    coord = data.get("coordinator", {})
    agents = data.get("agents", {})
    safe = data.get("safe_actions", {})
    verdict = data.get("verdict", {}).get("store", {})
    ctx_store = data.get("context_store", {})
    topo = data.get("topology", {})
    server = data.get("server", {})
    approval = data.get("approval", {})
    slack = data.get("slack", {})
    notifications = data.get("notifications", {})

    poll_interval = coord.get("poll_interval_seconds", 30)
    escalation_thresh = coord.get("escalation_threshold", 0.3)
    if not isinstance(poll_interval, (int, float)) or poll_interval <= 0:
        raise ValueError(f"poll_interval_seconds must be a positive number, got {poll_interval!r}")
    if not isinstance(escalation_thresh, (int, float)) or not (0.0 <= escalation_thresh <= 1.0):
        raise ValueError(f"escalation_threshold must be between 0.0 and 1.0, got {escalation_thresh!r}")

    return RespondConfig(
        poll_interval_seconds=int(poll_interval),
        escalation_threshold=float(escalation_thresh),
        model=agents.get("model", "claude-sonnet-4-20250514"),
        max_tokens=agents.get("max_tokens", 4096),
        triage_timeout=agents.get("triage", {}).get("timeout", 15),
        investigation_timeout=agents.get("investigation", {}).get("timeout", 60),
        communication_timeout=agents.get("communication", {}).get("timeout", 20),
        remediation_timeout=agents.get("remediation", {}).get("timeout", 30),
        root_cause_threshold=agents.get("investigation", {}).get("root_cause_threshold", 0.7),
        cooldown_seconds=safe.get("cooldown_seconds", 300),
        arbiter_url=safe.get("arbiter_url", "http://localhost:8080"),
        verdict_store_path=verdict.get("path", "verdicts.db"),
        context_store_path=ctx_store.get("path", "respond-incidents.db"),
        manifests_dir=topo.get("manifests_dir"),
        server_host=server.get("host", "0.0.0.0"),
        server_port=int(server.get("port", 8090)),
        approval_timeout_seconds=int(approval.get("timeout_seconds", 900)),
        slack_signing_secret=slack.get("signing_secret", ""),
        slack_bot_token=slack.get("bot_token", ""),
        ntfy_server_url=notifications.get("ntfy", {}).get("server_url", ""),
        ntfy_auth_token=notifications.get("ntfy", {}).get("auth_token", ""),
        twilio_account_sid=notifications.get("twilio", {}).get("account_sid", ""),
        twilio_auth_token=notifications.get("twilio", {}).get("auth_token", ""),
        twilio_from_number=notifications.get("twilio", {}).get("from_number", ""),
        pagerduty_routing_key=notifications.get("pagerduty", {}).get("routing_key", ""),
        webhook_base_url=notifications.get("webhook", {}).get("public_url", "http://localhost:8090"),
    )
