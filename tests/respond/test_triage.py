# tests/test_triage.py
"""Tests for triage agent."""
import json
from unittest.mock import AsyncMock, patch

import pytest
from nthlayer_common.verdicts import create as verdict_create

from nthlayer_workers.respond.agents.triage import TriageAgent
from nthlayer_workers.respond.types import AgentRole, IncidentContext, IncidentState


@pytest.fixture
def triage_agent(verdict_store):
    return TriageAgent(
        model="test-model",
        max_tokens=100,
        verdict_store=verdict_store,
        config={"arbiter_url": "http://localhost:8080"},
    )


@pytest.fixture
def sitrep_context(verdict_store):
    # Create a SitRep verdict in the store
    v = verdict_create(
        subject={"type": "correlation", "service": "payment-api", "ref": "cg-001", "summary": "Latency spike correlated with deploy"},
        judgment={"action": "flag", "confidence": 0.82, "reasoning": "Strong temporal correlation"},
        producer={"system": "nthlayer-correlate"},
    )
    verdict_store.put(v)
    return IncidentContext(
        id="INC-2026-0001",
        state=IncidentState.TRIGGERED,
        created_at="2026-03-19T10:00:00Z",
        updated_at="2026-03-19T10:00:00Z",
        trigger_source="nthlayer-correlate",
        trigger_verdict_ids=[v.id],
        topology={"services": [{"name": "payment-api", "tier": "critical"}]},
    )


@pytest.fixture
def pagerduty_context():
    return IncidentContext(
        id="INC-2026-0002",
        state=IncidentState.TRIGGERED,
        created_at="2026-03-19T10:00:00Z",
        updated_at="2026-03-19T10:00:00Z",
        trigger_source="pagerduty",
        trigger_verdict_ids=[],
        topology={},
    )


def test_build_prompt_sitrep_source(triage_agent, sitrep_context):
    system, user = triage_agent.build_prompt(sitrep_context)
    assert "10%" in system or "reversal" in system.lower()
    assert "pre-correlated" in user.lower() or "nthlayer-correlate" in user.lower()
    assert "payment-api" in user


def test_build_prompt_pagerduty_source(triage_agent, pagerduty_context):
    system, user = triage_agent.build_prompt(pagerduty_context)
    assert "no pre-correlation" in user.lower() or "raw alert" in user.lower()


def test_system_prompt_includes_slo(triage_agent, sitrep_context):
    system, _ = triage_agent.build_prompt(sitrep_context)
    assert "10%" in system


def test_parse_response_valid(triage_agent, sitrep_context):
    response = json.dumps({
        "severity": 1,
        "blast_radius": ["payment-api", "checkout-service"],
        "affected_slos": ["availability", "latency_p99"],
        "assigned_team": "payments-oncall",
        "reasoning": "Critical payment service affected",
    })
    result = triage_agent.parse_response(response, sitrep_context)
    assert result.severity == 1
    assert len(result.blast_radius) == 2
    assert result.assigned_team == "payments-oncall"


def test_parse_response_invalid_severity(triage_agent, sitrep_context):
    response = json.dumps({
        "severity": 10,  # out of range
        "blast_radius": ["svc"],
        "reasoning": "test",
    })
    result = triage_agent.parse_response(response, sitrep_context)
    assert 0 <= result.severity <= 4  # should be clamped


def test_parse_response_missing_blast_radius(triage_agent, sitrep_context):
    response = json.dumps({
        "severity": 2,
        "reasoning": "test",
    })
    result = triage_agent.parse_response(response, sitrep_context)
    assert isinstance(result.blast_radius, list)


def test_role_and_timeout(triage_agent):
    assert triage_agent.role == AgentRole.TRIAGE
    assert triage_agent.default_timeout == 15


def test_apply_result_sets_triage(triage_agent, sitrep_context):
    from nthlayer_workers.respond.types import TriageResult
    result = TriageResult(
        severity=2,
        blast_radius=["payment-api"],
        affected_slos=["availability"],
        assigned_team="platform-oncall",
        reasoning="Service degraded",
    )
    updated = triage_agent._apply_result(sitrep_context, result)
    assert updated.triage is result


def test_build_prompt_includes_topology(triage_agent, sitrep_context):
    _, user = triage_agent.build_prompt(sitrep_context)
    assert "payment-api" in user


def test_build_prompt_manual_source(triage_agent):
    context = IncidentContext(
        id="INC-2026-0003",
        state=IncidentState.TRIGGERED,
        created_at="2026-03-19T10:00:00Z",
        updated_at="2026-03-19T10:00:00Z",
        trigger_source="manual",
        trigger_verdict_ids=[],
        topology={"services": [{"name": "auth-service"}]},
    )
    system, user = triage_agent.build_prompt(context)
    assert "no pre-correlation" in user.lower() or "raw alert" in user.lower()


def test_parse_response_affected_slos_defaults(triage_agent, sitrep_context):
    response = json.dumps({
        "severity": 3,
        "blast_radius": ["svc-a"],
        "reasoning": "minor issue",
    })
    result = triage_agent.parse_response(response, sitrep_context)
    assert isinstance(result.affected_slos, list)


def test_parse_response_severity_negative_clamped(triage_agent, sitrep_context):
    response = json.dumps({
        "severity": -5,
        "blast_radius": [],
        "reasoning": "test",
    })
    result = triage_agent.parse_response(response, sitrep_context)
    assert result.severity == 0


async def test_post_execute_triggers_autonomy_reduction(triage_agent, verdict_store):
    """_post_execute calls _request_autonomy_reduction when trigger verdict
    has 'agent_model_update' tag AND severity <= 2."""
    v = verdict_create(
        subject={"type": "correlation", "service": "api", "ref": "cg-002", "summary": "Model drift"},
        judgment={"action": "flag", "confidence": 0.9, "reasoning": "drift", "tags": ["agent_model_update"]},
        producer={"system": "nthlayer-correlate"},
    )
    verdict_store.put(v)
    context = IncidentContext(
        id="INC-2026-0010",
        state=IncidentState.TRIGGERED,
        created_at="2026-03-19T10:00:00Z",
        updated_at="2026-03-19T10:00:00Z",
        trigger_source="nthlayer-correlate",
        trigger_verdict_ids=[v.id],
        topology={},
    )
    from nthlayer_workers.respond.types import TriageResult
    result = TriageResult(
        severity=1,
        blast_radius=[],
        affected_slos=[],
        assigned_team=None,
        reasoning="low severity model update",
    )
    with patch.object(triage_agent, "_request_autonomy_reduction", new_callable=AsyncMock, return_value={"status": "ok"}) as mock_reduce:
        await triage_agent._post_execute(context, result)
    mock_reduce.assert_called_once()
    call_args = mock_reduce.call_args
    assert call_args.kwargs["arbiter_url"] == "http://localhost:8080"


async def test_post_execute_no_autonomy_reduction_high_severity(triage_agent, verdict_store):
    """_post_execute does NOT call _request_autonomy_reduction when severity > 2."""
    v = verdict_create(
        subject={"type": "correlation", "service": "api", "ref": "cg-003", "summary": "Model drift"},
        judgment={"action": "flag", "confidence": 0.9, "reasoning": "drift", "tags": ["agent_model_update"]},
        producer={"system": "nthlayer-correlate"},
    )
    verdict_store.put(v)
    context = IncidentContext(
        id="INC-2026-0011",
        state=IncidentState.TRIGGERED,
        created_at="2026-03-19T10:00:00Z",
        updated_at="2026-03-19T10:00:00Z",
        trigger_source="nthlayer-correlate",
        trigger_verdict_ids=[v.id],
        topology={},
    )
    from nthlayer_workers.respond.types import TriageResult
    result = TriageResult(
        severity=3,
        blast_radius=[],
        affected_slos=[],
        assigned_team=None,
        reasoning="high severity — not a model update scenario",
    )
    with patch.object(triage_agent, "_request_autonomy_reduction", new_callable=AsyncMock) as mock_reduce:
        await triage_agent._post_execute(context, result)
    mock_reduce.assert_not_called()


async def test_post_execute_no_autonomy_reduction_no_tag(triage_agent, verdict_store):
    """_post_execute does NOT call _request_autonomy_reduction when tag absent."""
    v = verdict_create(
        subject={"type": "correlation", "service": "api", "ref": "cg-004", "summary": "Normal alert"},
        judgment={"action": "flag", "confidence": 0.9, "reasoning": "normal"},
        producer={"system": "nthlayer-correlate"},
    )
    verdict_store.put(v)
    context = IncidentContext(
        id="INC-2026-0012",
        state=IncidentState.TRIGGERED,
        created_at="2026-03-19T10:00:00Z",
        updated_at="2026-03-19T10:00:00Z",
        trigger_source="nthlayer-correlate",
        trigger_verdict_ids=[v.id],
        topology={},
    )
    from nthlayer_workers.respond.types import TriageResult
    result = TriageResult(
        severity=1,
        blast_radius=[],
        affected_slos=[],
        assigned_team=None,
        reasoning="low severity, no model update tag",
    )
    with patch.object(triage_agent, "_request_autonomy_reduction", new_callable=AsyncMock) as mock_reduce:
        await triage_agent._post_execute(context, result)
    mock_reduce.assert_not_called()
