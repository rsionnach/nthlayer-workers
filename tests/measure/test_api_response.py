"""Tests for API response builder."""
from nthlayer_common.verdicts import create as verdict_create

from nthlayer_workers.measure.api.response import build_error_response, build_response


def _make_verdict(dims=None, score=0.85, confidence=0.78):
    return verdict_create(
        subject={"type": "agent_output", "ref": "task-1", "summary": "test eval"},
        judgment={
            "action": "approve",
            "score": score,
            "confidence": confidence,
            "dimensions": dims or {"correctness": 0.9},
            "reasoning": "Looks good",
        },
        producer={"system": "arbiter", "model": "test-model"},
    )


def test_response_has_all_keys():
    v = _make_verdict()
    resp = build_response(v)
    assert "verdict_id" in resp
    assert resp["action"] == "approve"
    assert resp["score"] == 0.85
    assert resp["confidence"] == 0.78
    assert resp["dimensions"] == {"correctness": 0.9}
    assert resp["reasoning"] == "Looks good"
    assert resp["risk_tier"] == "standard"


def test_verdict_without_dimensions():
    v = verdict_create(
        subject={"type": "agent_output", "ref": "t1", "summary": "test"},
        judgment={"action": "reject", "confidence": 0.5},
        producer={"system": "arbiter"},
    )
    resp = build_response(v)
    assert resp["dimensions"] == {}


def test_with_governance():
    v = _make_verdict()
    gov = {"agent_status": "autonomous", "review_threshold": 0.7, "error_budget_remaining": 0.82}
    resp = build_response(v, governance=gov)
    assert resp["governance"] == gov


def test_without_governance_no_key():
    v = _make_verdict()
    resp = build_response(v)
    assert "governance" not in resp


def test_build_error_response():
    resp = build_error_response(422, "Missing field")
    assert resp["error"] == "Missing field"
    assert resp["status"] == 422
    assert "details" not in resp


def test_build_error_response_with_details():
    resp = build_error_response(400, "Bad request", details={"field": "agent"})
    assert resp["details"] == {"field": "agent"}
