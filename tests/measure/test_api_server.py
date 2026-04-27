"""Tests for the FastAPI HTTP API server."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from nthlayer_common.verdicts import MemoryStore, create as verdict_create
from nthlayer_workers.measure.api.server import create_app
from nthlayer_workers.measure.types import AutonomyLevel, QualityScore, TrendWindow


def _make_score(agent="test-agent", task_id="task-1"):
    return QualityScore(
        eval_id="eval-001",
        agent_name=agent,
        task_id=task_id,
        dimensions={"correctness": 0.9, "completeness": 0.8},
        reasoning={"correctness": "Good", "completeness": "Decent"},
        confidence=0.85,
        evaluator_model="test-model",
    )


def _make_trend(agent="test-agent"):
    return TrendWindow(
        agent_name=agent,
        window_days=7,
        dimension_averages={"correctness": 0.85},
        evaluation_count=10,
        confidence_mean=0.8,
        reversal_rate=0.03,
    )


@pytest.fixture
def mock_evaluator():
    evaluator = AsyncMock()
    evaluator.evaluate = AsyncMock(return_value=_make_score())
    return evaluator


@pytest.fixture
def mock_store():
    store = AsyncMock()
    store.save_score = AsyncMock()
    store.set_verdict_id = AsyncMock()
    return store


@pytest.fixture
def mock_tracker():
    tracker = AsyncMock()
    tracker.compute_window = AsyncMock(return_value=_make_trend())
    return tracker


@pytest.fixture
def mock_governance():
    gov = AsyncMock()
    gov.get_autonomy = AsyncMock(return_value=AutonomyLevel.FULL)
    return gov


@pytest.fixture
def verdict_store():
    return MemoryStore()


@pytest.fixture
def client(mock_evaluator, mock_store, mock_tracker, verdict_store):
    app = create_app(
        evaluator=mock_evaluator,
        store=mock_store,
        tracker=mock_tracker,
        dimensions=["correctness", "completeness"],
        verdict_store=verdict_store,
        sync_timeout=5.0,
        max_workers=1,
    )
    return TestClient(app)


@pytest.fixture
def client_with_governance(mock_evaluator, mock_store, mock_tracker, mock_governance, verdict_store):
    app = create_app(
        evaluator=mock_evaluator,
        store=mock_store,
        tracker=mock_tracker,
        dimensions=["correctness", "completeness"],
        governance=mock_governance,
        verdict_store=verdict_store,
        sync_timeout=5.0,
        max_workers=1,
    )
    return TestClient(app)


# ------------------------------------------------------------------ #
# Health                                                               #
# ------------------------------------------------------------------ #

def test_health(client):
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ------------------------------------------------------------------ #
# Level 1: Fire and forget                                             #
# ------------------------------------------------------------------ #

def test_evaluate_async_returns_202(client):
    resp = client.post("/api/v1/evaluate", json={
        "agent": "code-reviewer",
        "output": "Looks good, approved.",
    })
    assert resp.status_code == 202
    data = resp.json()
    assert "evaluation_id" in data
    assert data["status"] == "queued"
    assert "poll_url" in data


def test_evaluate_async_missing_agent(client):
    resp = client.post("/api/v1/evaluate", json={"output": "hello"})
    assert resp.status_code == 422
    assert "agent" in resp.json()["error"]


def test_evaluate_async_missing_output(client):
    resp = client.post("/api/v1/evaluate", json={"agent": "test"})
    assert resp.status_code == 422
    assert "output" in resp.json()["error"]


# ------------------------------------------------------------------ #
# Level 2: Synchronous gate                                            #
# ------------------------------------------------------------------ #

def test_evaluate_sync_returns_verdict(client, mock_evaluator):
    resp = client.post("/api/v1/evaluate/sync", json={
        "agent": "code-reviewer",
        "output": "Approved.",
        "context": "Review PR #1234",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "verdict_id" in data
    assert data["action"] in ("approve", "reject")
    assert "confidence" in data
    assert "dimensions" in data
    mock_evaluator.evaluate.assert_called_once()


def test_evaluate_sync_missing_fields(client):
    resp = client.post("/api/v1/evaluate/sync", json={"agent": "test"})
    assert resp.status_code == 422


def test_evaluate_sync_timeout_returns_408():
    """Timeout falls back to async queue with 408."""
    async def slow_evaluate(output, dims, model=None):
        await asyncio.sleep(10)
        return _make_score()

    slow_evaluator = AsyncMock()
    slow_evaluator.evaluate = slow_evaluate
    app = create_app(
        evaluator=slow_evaluator,
        store=AsyncMock(),
        tracker=AsyncMock(),
        dimensions=["correctness"],
        sync_timeout=0.1,
        max_workers=1,
    )
    slow_client = TestClient(app)
    resp = slow_client.post("/api/v1/evaluate/sync", json={
        "agent": "test", "output": "hello",
    })
    assert resp.status_code == 408
    data = resp.json()
    assert data["status"] == "timeout"
    assert "Retry" in data["message"]


# ------------------------------------------------------------------ #
# Poll for async result                                                #
# ------------------------------------------------------------------ #

def test_poll_nonexistent(client):
    resp = client.get("/api/v1/evaluations/eval-nonexistent")
    assert resp.status_code == 404


def test_poll_after_submit(client):
    """Submit async, poll — result should be queued or complete."""
    submit_resp = client.post("/api/v1/evaluate", json={
        "agent": "test", "output": "hello",
    })
    eval_id = submit_resp.json()["evaluation_id"]

    poll_resp = client.get(f"/api/v1/evaluations/{eval_id}")
    assert poll_resp.status_code == 200
    data = poll_resp.json()
    # TestClient runs sync, so queue worker may not have processed yet
    assert data["status"] in ("queued", "evaluating", "complete")


# ------------------------------------------------------------------ #
# Override and confirm                                                  #
# ------------------------------------------------------------------ #

def test_override_pending_verdict(client, verdict_store):
    v = verdict_create(
        subject={"type": "agent_output", "ref": "t1", "summary": "test"},
        judgment={"action": "approve", "confidence": 0.8},
        producer={"system": "arbiter"},
    )
    verdict_store.put(v)

    resp = client.post("/api/v1/override", json={
        "verdict_id": v.id,
        "actor": "human:rob",
        "reasoning": "Missed rate limiting",
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "overridden"


def test_override_missing_verdict(client):
    resp = client.post("/api/v1/override", json={
        "verdict_id": "vrd-nonexistent",
        "actor": "human:rob",
    })
    assert resp.status_code == 404


def test_override_already_resolved(client, verdict_store):
    v = verdict_create(
        subject={"type": "agent_output", "ref": "t1", "summary": "test"},
        judgment={"action": "approve", "confidence": 0.8},
        producer={"system": "arbiter"},
    )
    verdict_store.put(v)
    verdict_store.resolve(v.id, "confirmed")

    resp = client.post("/api/v1/override", json={
        "verdict_id": v.id,
        "actor": "human:rob",
    })
    assert resp.status_code == 409


def test_override_missing_fields(client):
    resp = client.post("/api/v1/override", json={"verdict_id": "vrd-123"})
    assert resp.status_code == 422


def test_confirm_verdict(client, verdict_store):
    v = verdict_create(
        subject={"type": "agent_output", "ref": "t1", "summary": "test"},
        judgment={"action": "approve", "confidence": 0.8},
        producer={"system": "arbiter"},
    )
    verdict_store.put(v)

    resp = client.post("/api/v1/confirm", json={
        "verdict_id": v.id,
        "actor": "human:rob",
        "reasoning": "Judgment was correct",
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "confirmed"


def test_resolve_batch(client, verdict_store):
    v1 = verdict_create(
        subject={"type": "agent_output", "ref": "t1", "summary": "test1"},
        judgment={"action": "approve", "confidence": 0.8},
        producer={"system": "arbiter"},
    )
    v2 = verdict_create(
        subject={"type": "agent_output", "ref": "t2", "summary": "test2"},
        judgment={"action": "reject", "confidence": 0.4},
        producer={"system": "arbiter"},
    )
    verdict_store.put(v1)
    verdict_store.put(v2)

    resp = client.post("/api/v1/resolve/batch", json={
        "resolutions": [
            {"verdict_id": v1.id, "status": "confirmed", "actor": "rob"},
            {"verdict_id": v2.id, "status": "overridden", "actor": "rob", "reasoning": "Was actually ok"},
            {"verdict_id": "vrd-missing", "status": "confirmed", "actor": "rob"},
        ],
    })
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert results[0]["status"] == "confirmed"
    assert results[1]["status"] == "overridden"
    assert results[2]["status"] == "error"


# ------------------------------------------------------------------ #
# Query endpoints                                                      #
# ------------------------------------------------------------------ #

def test_agent_accuracy(client, verdict_store):
    # Seed some verdicts
    for i in range(3):
        v = verdict_create(
            subject={"type": "agent_output", "ref": f"t{i}", "summary": f"test{i}", "agent": "code-reviewer"},
            judgment={"action": "approve", "confidence": 0.8},
            producer={"system": "arbiter"},
        )
        verdict_store.put(v)

    resp = client.get("/api/v1/agents/code-reviewer/accuracy?window=7d")
    assert resp.status_code == 200
    data = resp.json()
    assert data["agent"] == "code-reviewer"
    assert data["total_verdicts"] >= 0


def test_agent_verdicts(client, verdict_store):
    v = verdict_create(
        subject={"type": "agent_output", "ref": "t1", "summary": "test", "agent": "test-agent"},
        judgment={"action": "approve", "confidence": 0.8, "score": 0.85, "dimensions": {"correctness": 0.9}},
        producer={"system": "arbiter"},
    )
    verdict_store.put(v)

    resp = client.get("/api/v1/agents/test-agent/verdicts?limit=10")
    assert resp.status_code == 200
    data = resp.json()
    assert "verdicts" in data


def test_governance_status(client_with_governance, mock_governance, mock_tracker):
    resp = client_with_governance.get("/api/v1/governance/code-reviewer")
    assert resp.status_code == 200
    data = resp.json()
    assert data["agent"] == "code-reviewer"
    assert data["status"] == "full"
    assert "reversal_rate" in data


def test_governance_not_configured(client):
    """No governance engine → 503."""
    resp = client.get("/api/v1/governance/code-reviewer")
    assert resp.status_code == 503


# ------------------------------------------------------------------ #
# No verdict store                                                     #
# ------------------------------------------------------------------ #

def test_override_without_verdict_store():
    app = create_app(
        evaluator=AsyncMock(),
        store=AsyncMock(),
        tracker=AsyncMock(),
        dimensions=["correctness"],
        verdict_store=None,
    )
    c = TestClient(app)
    resp = c.post("/api/v1/override", json={
        "verdict_id": "vrd-123", "actor": "rob",
    })
    assert resp.status_code == 503


def test_evaluate_invalid_json_body(client):
    """Malformed JSON body returns 422, not 500."""
    resp = client.post(
        "/api/v1/evaluate",
        content=b"{broken json",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 422
    assert "Invalid JSON" in resp.json()["error"]


def test_evaluate_sync_minimal_tier_auto_approves(mock_evaluator, mock_store, mock_tracker):
    """Minimal tier with 0% sampling skips model call entirely."""
    from nthlayer_workers.measure.config import TieringConfig
    from nthlayer_workers.measure.tiering.classifier import TierClassifier

    config = TieringConfig(enabled=True, default_tier="minimal", sampling_rate=0.0)
    classifier = TierClassifier(config, manifests={})

    app = create_app(
        evaluator=mock_evaluator,
        store=mock_store,
        tracker=mock_tracker,
        dimensions=["correctness"],
        sync_timeout=5.0,
        max_workers=1,
        classifier=classifier,
    )
    c = TestClient(app)
    resp = c.post("/api/v1/evaluate/sync", json={
        "agent": "test-agent", "output": "hello",
    })
    assert resp.status_code == 200
    mock_evaluator.evaluate.assert_not_called()


def test_evaluate_sync_without_verdict_store(mock_evaluator, mock_store, mock_tracker):
    """Sync eval without verdict store returns score-based response."""
    app = create_app(
        evaluator=mock_evaluator,
        store=mock_store,
        tracker=mock_tracker,
        dimensions=["correctness", "completeness"],
        verdict_store=None,
        sync_timeout=5.0,
        max_workers=1,
    )
    c = TestClient(app)
    resp = c.post("/api/v1/evaluate/sync", json={
        "agent": "test-agent", "output": "hello",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "eval_id" in data
    assert data["action"] in ("approve", "reject")
    assert "dimensions" in data
    assert "confidence" in data
