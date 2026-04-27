"""Tests for Slack block builders."""
from __future__ import annotations

from unittest.mock import MagicMock

from nthlayer_workers.respond.notifications import (
    build_approval_blocks,
)


def _make_verdict(summary="rollback on fraud-detect", confidence=0.85, verdict_id="vrd-123"):
    v = MagicMock()
    v.id = verdict_id
    v.subject.summary = summary
    v.judgment.confidence = confidence
    return v


def test_build_approval_blocks_has_actions():
    """build_approval_blocks includes approve/reject buttons."""
    verdict = _make_verdict()
    blocks, text = build_approval_blocks(verdict, "INC-FRAUD-20260403")

    action_blocks = [b for b in blocks if b.get("type") == "actions"]
    assert len(action_blocks) == 1

    elements = action_blocks[0]["elements"]
    assert len(elements) == 2

    approve_btn = elements[0]
    assert approve_btn["action_id"] == "approve"
    assert approve_btn["value"] == "INC-FRAUD-20260403"
    assert approve_btn["style"] == "primary"

    reject_btn = elements[1]
    assert reject_btn["action_id"] == "reject"
    assert reject_btn["value"] == "INC-FRAUD-20260403"
    assert reject_btn["style"] == "danger"


def test_build_approval_blocks_includes_remediation_info():
    """build_approval_blocks includes the remediation summary."""
    verdict = _make_verdict(summary="rollback on fraud-detect (requires approval)")
    blocks, text = build_approval_blocks(verdict, "INC-FRAUD-20260403")

    section_texts = [
        b["text"]["text"]
        for b in blocks
        if b.get("type") == "section" and "text" in b
    ]
    assert any("rollback on fraud-detect" in t for t in section_texts)


def test_build_approval_blocks_block_id_contains_incident():
    """Actions block_id contains the incident ID for routing."""
    verdict = _make_verdict()
    blocks, _ = build_approval_blocks(verdict, "INC-FRAUD-20260403")

    action_blocks = [b for b in blocks if b.get("type") == "actions"]
    assert action_blocks[0]["block_id"] == "approval_INC-FRAUD-20260403"


def test_build_approval_blocks_fallback_text():
    """Fallback text is descriptive."""
    verdict = _make_verdict()
    _, text = build_approval_blocks(verdict, "INC-FRAUD-20260403")
    assert "APPROVAL REQUIRED" in text


def test_resolve_slack_channel_from_context():
    """resolve_slack_channel reads from service context manifest ownership."""
    from nthlayer_workers.respond.notifications import resolve_slack_channel

    context = MagicMock()
    context.metadata = {
        "service_context": {
            "spec": {
                "ownership": {"slack_channel": "C-PAYMENTS"},
            }
        }
    }
    assert resolve_slack_channel(context) == "C-PAYMENTS"


def test_resolve_slack_channel_fallback_to_env(monkeypatch):
    """resolve_slack_channel falls back to SLACK_CHANNEL_ID env var."""
    from nthlayer_workers.respond.notifications import resolve_slack_channel

    monkeypatch.setenv("SLACK_CHANNEL_ID", "C-DEFAULT")
    context = MagicMock()
    context.metadata = {}
    assert resolve_slack_channel(context) == "C-DEFAULT"


def test_resolve_slack_channel_returns_none():
    """resolve_slack_channel returns None when no channel configured."""
    from nthlayer_workers.respond.notifications import resolve_slack_channel

    context = MagicMock()
    context.metadata = {}
    assert resolve_slack_channel(context, env_fallback="") is None


def test_resolve_slack_channel_from_notifications_section():
    """resolve_slack_channel reads from spec.notifications.slack.channel_id first."""
    from nthlayer_workers.respond.notifications import resolve_slack_channel

    context = MagicMock()
    context.metadata = {
        "service_context": {
            "spec": {
                "notifications": {
                    "slack": {"channel_id": "C-FROM-NOTIFICATIONS"},
                },
                "ownership": {"slack_channel": "C-FROM-OWNERSHIP"},
            }
        }
    }
    assert resolve_slack_channel(context) == "C-FROM-NOTIFICATIONS"


def test_resolve_slack_channel_falls_back_to_ownership():
    """resolve_slack_channel falls back to ownership.slack_channel when no notifications section."""
    from nthlayer_workers.respond.notifications import resolve_slack_channel

    context = MagicMock()
    context.metadata = {
        "service_context": {
            "spec": {
                "ownership": {"slack_channel": "C-FROM-OWNERSHIP"},
            }
        }
    }
    assert resolve_slack_channel(context) == "C-FROM-OWNERSHIP"
