"""Tests for ApprovalServer HTTP routes."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import urllib.parse
from datetime import UTC
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from nthlayer_workers.respond.config import RespondConfig
from nthlayer_workers.respond.context_store import SQLiteContextStore
from nthlayer_workers.respond.server import ApprovalServer
from nthlayer_workers.respond.types import (
    IncidentContext,
    IncidentState,
    RemediationResult,
)


@pytest.fixture
def context_store(tmp_path):
    s = SQLiteContextStore(str(tmp_path / "test.db"))
    yield s
    s.close()


@pytest.fixture
def mock_coordinator():
    coord = AsyncMock()
    return coord


@pytest.fixture
def config():
    return RespondConfig(approval_timeout_seconds=900)


@pytest.fixture
def server(mock_coordinator, context_store, config):
    from nthlayer_common.verdicts import MemoryStore
    verdict_store = MemoryStore()
    return ApprovalServer(mock_coordinator, context_store, config, verdict_store=verdict_store)


@pytest.fixture
def client(server):
    return TestClient(server.build_app())


def _awaiting_context(incident_id="INC-TEST-001"):
    return IncidentContext(
        id=incident_id,
        state=IncidentState.AWAITING_APPROVAL,
        created_at="2026-04-03T10:00:00Z",
        updated_at="2026-04-03T10:00:00Z",
        trigger_source="nthlayer-correlate",
        trigger_verdict_ids=["vrd-trigger"],
        topology={},
        remediation=RemediationResult(
            proposed_action="rollback",
            target="fraud-detect",
            requires_human_approval=True,
            reasoning="needs approval",
        ),
        verdict_chain=["vrd-triage", "vrd-investigation", "vrd-remediation"],
    )


def test_approve_success(client, mock_coordinator, context_store):
    """POST /api/v1/incidents/{id}/approve calls coordinator.approve."""
    ctx = _awaiting_context()
    context_store.save(ctx)

    resolved_ctx = _awaiting_context()
    resolved_ctx.state = IncidentState.RESOLVED
    resolved_ctx.verdict_chain.append("vrd-approved")
    mock_coordinator.approve = AsyncMock(return_value=resolved_ctx)

    resp = client.post(
        "/api/v1/incidents/INC-TEST-001/approve",
        json={"approved_by": "rob@nthlayer.com"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "resolved"
    assert data["approved_by"] == "rob@nthlayer.com"
    mock_coordinator.approve.assert_called_once_with(
        "INC-TEST-001", approved_by="rob@nthlayer.com"
    )


def test_approve_wrong_state(client, mock_coordinator, context_store):
    """POST approve on non-AWAITING_APPROVAL returns 409."""
    ctx = _awaiting_context()
    ctx.state = IncidentState.RESOLVED
    context_store.save(ctx)

    mock_coordinator.approve = AsyncMock(
        side_effect=ValueError("not AWAITING_APPROVAL")
    )

    resp = client.post(
        "/api/v1/incidents/INC-TEST-001/approve",
        json={"approved_by": "rob"},
    )
    assert resp.status_code == 409


def test_approve_not_found(client, mock_coordinator):
    """POST approve on nonexistent incident returns 404."""
    mock_coordinator.approve = AsyncMock(
        side_effect=ValueError("not found")
    )

    resp = client.post(
        "/api/v1/incidents/INC-MISSING/approve",
        json={},
    )
    assert resp.status_code == 404


def test_reject_success(client, mock_coordinator, context_store):
    """POST /api/v1/incidents/{id}/reject calls coordinator.reject."""
    ctx = _awaiting_context()
    context_store.save(ctx)

    escalated_ctx = _awaiting_context()
    escalated_ctx.state = IncidentState.ESCALATED
    mock_coordinator.reject = AsyncMock(return_value=escalated_ctx)

    resp = client.post(
        "/api/v1/incidents/INC-TEST-001/reject",
        json={"reason": "Wrong target", "rejected_by": "rob@nthlayer.com"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "escalated"
    mock_coordinator.reject.assert_called_once_with(
        "INC-TEST-001", "Wrong target", rejected_by="rob@nthlayer.com"
    )


def test_reject_missing_reason(client, mock_coordinator, context_store):
    """POST reject without reason returns 400."""
    ctx = _awaiting_context()
    context_store.save(ctx)

    resp = client.post(
        "/api/v1/incidents/INC-TEST-001/reject",
        json={},
    )
    assert resp.status_code == 400


def test_get_incident_status(client, context_store):
    """GET /api/v1/incidents/{id} returns incident state."""
    ctx = _awaiting_context()
    context_store.save(ctx)

    resp = client.get("/api/v1/incidents/INC-TEST-001")
    assert resp.status_code == 200
    data = resp.json()
    assert data["incident_id"] == "INC-TEST-001"
    assert data["state"] == "awaiting_approval"
    assert data["proposed_action"] == "rollback"
    assert data["target"] == "fraud-detect"


def test_get_incident_not_found(client):
    """GET nonexistent incident returns 404."""
    resp = client.get("/api/v1/incidents/INC-MISSING")
    assert resp.status_code == 404


# --- Slack interaction tests ---


def _make_slack_signature(secret: str, body: bytes) -> tuple[str, str]:
    """Generate a valid Slack signature and timestamp."""
    timestamp = str(int(time.time()))
    sig_basestring = f"v0:{timestamp}:{body.decode()}"
    sig = "v0=" + hmac.new(
        secret.encode(), sig_basestring.encode(), hashlib.sha256
    ).hexdigest()
    return timestamp, sig


@pytest.fixture
def config_with_slack():
    return RespondConfig(
        approval_timeout_seconds=900,
        slack_signing_secret="test-signing-secret",
        slack_bot_token="xoxb-test-token",
    )


@pytest.fixture
def server_with_slack(mock_coordinator, context_store, config_with_slack):
    from nthlayer_common.verdicts import MemoryStore
    verdict_store = MemoryStore()
    return ApprovalServer(mock_coordinator, context_store, config_with_slack, verdict_store=verdict_store)


@pytest.fixture
def client_with_slack(server_with_slack):
    return TestClient(server_with_slack.build_app())


def test_slack_approve_interaction(
    client_with_slack, mock_coordinator, context_store
):
    """Slack approve button triggers coordinator.approve."""
    ctx = _awaiting_context()
    context_store.save(ctx)

    resolved_ctx = _awaiting_context()
    resolved_ctx.state = IncidentState.RESOLVED
    mock_coordinator.approve = AsyncMock(return_value=resolved_ctx)

    payload = json.dumps({
        "type": "block_actions",
        "user": {"id": "U12345", "name": "rob"},
        "actions": [{"action_id": "approve", "value": "INC-TEST-001"}],
        "channel": {"id": "C12345"},
        "message": {"ts": "1234567890.123456"},
    })
    body = f"payload={urllib.parse.quote(payload)}".encode()
    timestamp, signature = _make_slack_signature("test-signing-secret", body)

    resp = client_with_slack.post(
        "/api/v1/slack/interactions",
        content=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": timestamp,
            "X-Slack-Signature": signature,
        },
    )
    assert resp.status_code == 200
    mock_coordinator.approve.assert_called_once_with(
        "INC-TEST-001", approved_by="rob"
    )


def test_slack_reject_interaction(
    client_with_slack, mock_coordinator, context_store
):
    """Slack reject button triggers coordinator.reject."""
    ctx = _awaiting_context()
    context_store.save(ctx)

    escalated_ctx = _awaiting_context()
    escalated_ctx.state = IncidentState.ESCALATED
    mock_coordinator.reject = AsyncMock(return_value=escalated_ctx)

    payload = json.dumps({
        "type": "block_actions",
        "user": {"id": "U12345", "name": "rob"},
        "actions": [{"action_id": "reject", "value": "INC-TEST-001"}],
        "channel": {"id": "C12345"},
        "message": {"ts": "1234567890.123456"},
    })
    body = f"payload={urllib.parse.quote(payload)}".encode()
    timestamp, signature = _make_slack_signature("test-signing-secret", body)

    resp = client_with_slack.post(
        "/api/v1/slack/interactions",
        content=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": timestamp,
            "X-Slack-Signature": signature,
        },
    )
    assert resp.status_code == 200
    mock_coordinator.reject.assert_called_once_with(
        "INC-TEST-001", "Rejected via Slack by rob", rejected_by="rob"
    )


def test_slack_interaction_invalid_signature(client_with_slack):
    """Invalid Slack signature returns 401."""
    payload = json.dumps({"type": "block_actions", "actions": []})
    body = f"payload={urllib.parse.quote(payload)}".encode()

    resp = client_with_slack.post(
        "/api/v1/slack/interactions",
        content=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": str(int(time.time())),
            "X-Slack-Signature": "v0=invalid",
        },
    )
    assert resp.status_code == 401


def test_slack_interaction_no_signing_secret(client, mock_coordinator):
    """Without signing_secret configured, Slack endpoint returns 403."""
    payload = json.dumps({
        "type": "block_actions",
        "user": {"name": "rob"},
        "actions": [{"action_id": "approve", "value": "INC-TEST-001"}],
    })
    body = f"payload={urllib.parse.quote(payload)}".encode()

    resp = client.post(
        "/api/v1/slack/interactions",
        content=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 403


# --- Approval timeout tests ---


async def test_timeout_auto_rejects():
    """Timeout task calls coordinator.reject with system/timeout."""
    mock_coord = AsyncMock()
    escalated_ctx = _awaiting_context()
    escalated_ctx.state = IncidentState.ESCALATED
    mock_coord.reject = AsyncMock(return_value=escalated_ctx)

    store = MagicMock()
    ctx = _awaiting_context()
    store.load.return_value = ctx

    config = RespondConfig(approval_timeout_seconds=0)  # immediate timeout
    server = ApprovalServer(mock_coord, store, config)

    server.start_timeout("INC-TEST-001")
    await asyncio.sleep(0.1)  # Let the task fire

    mock_coord.reject.assert_called_once()
    call_args = mock_coord.reject.call_args
    assert call_args[0][0] == "INC-TEST-001"
    assert "timed out" in call_args[0][1].lower()
    assert call_args[1]["rejected_by"] == "system/timeout"


async def test_cancel_timeout_prevents_reject():
    """Cancelling timeout before it fires prevents rejection."""
    mock_coord = AsyncMock()
    store = MagicMock()
    store.load.return_value = _awaiting_context()

    config = RespondConfig(approval_timeout_seconds=10)
    server = ApprovalServer(mock_coord, store, config)

    server.start_timeout("INC-TEST-001")
    server.cancel_timeout("INC-TEST-001")
    await asyncio.sleep(0.1)

    mock_coord.reject.assert_not_called()


async def test_timeout_skips_already_resolved():
    """Timeout does nothing if incident is no longer AWAITING_APPROVAL."""
    mock_coord = AsyncMock()
    store = MagicMock()

    resolved_ctx = _awaiting_context()
    resolved_ctx.state = IncidentState.RESOLVED
    store.load.return_value = resolved_ctx

    config = RespondConfig(approval_timeout_seconds=0)
    server = ApprovalServer(mock_coord, store, config)

    server.start_timeout("INC-TEST-001")
    await asyncio.sleep(0.1)

    mock_coord.reject.assert_not_called()


# --- Malformed JSON body tests ---


def test_approve_malformed_json(client, mock_coordinator):
    """POST approve with invalid JSON body returns 400."""
    resp = client.post(
        "/api/v1/incidents/INC-TEST-001/approve",
        content=b"not valid json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert "Invalid JSON" in resp.json()["error"]


def test_reject_malformed_json(client, mock_coordinator):
    """POST reject with invalid JSON body returns 400."""
    resp = client.post(
        "/api/v1/incidents/INC-TEST-001/reject",
        content=b"{broken",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert "Invalid JSON" in resp.json()["error"]


# --- Recover pending approvals tests ---


async def test_recover_pending_approvals_starts_timeout():
    """recover_pending_approvals starts timeout for awaiting incidents."""
    mock_coord = AsyncMock()
    store = MagicMock()

    ctx = _awaiting_context()
    # Set updated_at to 1 second ago so there's plenty of remaining time
    from datetime import datetime
    ctx.updated_at = datetime.now(tz=UTC).isoformat()

    store.list_active.return_value = ["INC-TEST-001"]
    store.load.return_value = ctx

    config = RespondConfig(approval_timeout_seconds=300)
    server = ApprovalServer(mock_coord, store, config)

    await server.recover_pending_approvals()

    # Should have started a timeout
    assert "INC-TEST-001" in server._timeouts
    # Clean up
    server.cancel_timeout("INC-TEST-001")


async def test_recover_pending_approvals_expired_immediately_rejects():
    """recover_pending_approvals auto-rejects already-expired incidents."""
    mock_coord = AsyncMock()
    escalated = _awaiting_context()
    escalated.state = IncidentState.ESCALATED
    mock_coord.reject = AsyncMock(return_value=escalated)

    store = MagicMock()
    ctx = _awaiting_context()
    # Set updated_at to 20 minutes ago (well past 15 min timeout)
    from datetime import datetime, timedelta
    ctx.updated_at = (datetime.now(tz=UTC) - timedelta(minutes=20)).isoformat()

    store.list_active.return_value = ["INC-TEST-001"]
    store.load.return_value = ctx

    config = RespondConfig(approval_timeout_seconds=900)
    server = ApprovalServer(mock_coord, store, config)

    await server.recover_pending_approvals()

    mock_coord.reject.assert_called_once()
    call_args = mock_coord.reject.call_args
    assert "expired during server downtime" in call_args[0][1]


async def test_recover_pending_approvals_skips_non_awaiting():
    """recover_pending_approvals skips incidents not in AWAITING_APPROVAL."""
    mock_coord = AsyncMock()
    store = MagicMock()

    resolved_ctx = _awaiting_context()
    resolved_ctx.state = IncidentState.RESOLVED

    store.list_active.return_value = ["INC-TEST-001"]
    store.load.return_value = resolved_ctx

    config = RespondConfig(approval_timeout_seconds=300)
    server = ApprovalServer(mock_coord, store, config)

    await server.recover_pending_approvals()

    mock_coord.reject.assert_not_called()
    assert "INC-TEST-001" not in server._timeouts


def test_metrics_endpoint(client, context_store):
    """GET /metrics returns Prometheus text with HELP headers."""
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    assert "# " in resp.text  # Has comment/HELP headers


# --- Concurrent approve+timeout test ---


async def test_concurrent_approve_and_timeout_no_double_action():
    """Lock prevents concurrent approve and timeout from both executing."""
    mock_coord = AsyncMock()
    store = MagicMock()

    ctx = _awaiting_context()
    resolved_ctx = _awaiting_context()
    resolved_ctx.state = IncidentState.RESOLVED

    # First call succeeds (approve), second call sees RESOLVED state
    call_count = {"n": 0}
    original_ctx = ctx

    def load_side_effect(incident_id):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return original_ctx  # First load: still AWAITING_APPROVAL
        return resolved_ctx  # Second load: already resolved

    store.load.side_effect = load_side_effect
    mock_coord.approve = AsyncMock(return_value=resolved_ctx)

    config = RespondConfig(approval_timeout_seconds=0)
    server = ApprovalServer(mock_coord, store, config)

    # Start timeout (fires immediately with 0s delay)
    server.start_timeout("INC-TEST-001")

    # Approve via the lock - this should win the race
    async with server._get_lock("INC-TEST-001"):
        await server._coordinator.approve("INC-TEST-001", approved_by="rob")
        server.cancel_timeout("INC-TEST-001")

    await asyncio.sleep(0.1)

    # Coordinator approve was called but reject should NOT have been called
    # (timeout task either got cancelled or saw non-AWAITING state)
    mock_coord.reject.assert_not_called()
