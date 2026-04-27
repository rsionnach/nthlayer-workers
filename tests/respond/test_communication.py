# tests/test_communication.py
"""Tests for CommunicationAgent."""
from __future__ import annotations

import json
import pytest
from nthlayer_common.verdicts import MemoryStore

from nthlayer_workers.respond.agents.communication import CommunicationAgent
from nthlayer_workers.respond.types import (
    AgentRole,
    CommunicationResult,
    CommunicationUpdate,
    IncidentContext,
    IncidentState,
    InvestigationResult,
    RemediationResult,
    TriageResult,
)


# ------------------------------------------------------------------ #
# Fixtures                                                             #
# ------------------------------------------------------------------ #

@pytest.fixture
def verdict_store():
    return MemoryStore()


@pytest.fixture
def comm_agent(verdict_store):
    return CommunicationAgent(
        model="test-model",
        max_tokens=200,
        verdict_store=verdict_store,
        config={},
    )


@pytest.fixture
def triage_result():
    return TriageResult(
        severity=1,
        blast_radius=["payment-api", "checkout-service"],
        affected_slos=["availability", "latency_p99"],
        assigned_team="payments-oncall",
        reasoning="Critical payment service degraded",
    )


@pytest.fixture
def investigation_result():
    return InvestigationResult(
        hypotheses=[],
        root_cause="Database connection pool exhausted",
        root_cause_confidence=0.85,
        reasoning="Confirmed via metrics",
    )


@pytest.fixture
def remediation_result():
    return RemediationResult(
        proposed_action="restart_service",
        target="payment-api",
        risk_assessment="Low risk",
        requires_human_approval=False,
        executed=True,
        execution_result="Service restarted successfully",
        reasoning="Restart resolved connection pool",
    )


@pytest.fixture
def context_phase1(verdict_store, triage_result):
    """Phase 1: triage done, no remediation yet."""
    ctx = IncidentContext(
        id="INC-2026-0001",
        state=IncidentState.INVESTIGATING,
        created_at="2026-03-19T10:00:00Z",
        updated_at="2026-03-19T10:05:00Z",
        trigger_source="nthlayer-correlate",
        trigger_verdict_ids=[],
        topology={"services": [{"name": "payment-api"}]},
    )
    ctx.triage = triage_result
    return ctx


@pytest.fixture
def context_phase2(verdict_store, triage_result, investigation_result, remediation_result):
    """Phase 2: triage + investigation + remediation all done."""
    ctx = IncidentContext(
        id="INC-2026-0001",
        state=IncidentState.REMEDIATING,
        created_at="2026-03-19T10:00:00Z",
        updated_at="2026-03-19T10:15:00Z",
        trigger_source="nthlayer-correlate",
        trigger_verdict_ids=[],
        topology={"services": [{"name": "payment-api"}]},
    )
    ctx.triage = triage_result
    ctx.investigation = investigation_result
    ctx.remediation = remediation_result
    return ctx


# ------------------------------------------------------------------ #
# Role and timeout                                                     #
# ------------------------------------------------------------------ #

def test_role_and_timeout(comm_agent):
    assert comm_agent.role == AgentRole.COMMUNICATION
    assert comm_agent.default_timeout == 20


# ------------------------------------------------------------------ #
# build_prompt — phase 1 (initial)                                     #
# ------------------------------------------------------------------ #

def test_build_prompt_initial_phase(comm_agent, context_phase1):
    system, user = comm_agent.build_prompt(context_phase1)
    assert "initial status update" in user.lower()
    assert "investigation is ongoing" in user.lower()


def test_build_prompt_initial_phase_includes_severity(comm_agent, context_phase1):
    _, user = comm_agent.build_prompt(context_phase1)
    assert "1" in user  # severity from triage


def test_build_prompt_initial_phase_includes_blast_radius(comm_agent, context_phase1):
    _, user = comm_agent.build_prompt(context_phase1)
    assert "payment-api" in user
    assert "checkout-service" in user


def test_build_prompt_initial_phase_no_remediation_details(comm_agent, context_phase1):
    _, user = comm_agent.build_prompt(context_phase1)
    # Phase 1 must not mention resolution
    assert "resolution update" not in user.lower()


# ------------------------------------------------------------------ #
# build_prompt — phase 2 (resolution)                                  #
# ------------------------------------------------------------------ #

def test_build_prompt_resolution_phase(comm_agent, context_phase2):
    system, user = comm_agent.build_prompt(context_phase2)
    assert "resolution update" in user.lower()


def test_build_prompt_resolution_phase_includes_root_cause(comm_agent, context_phase2):
    _, user = comm_agent.build_prompt(context_phase2)
    assert "Database connection pool exhausted" in user


def test_build_prompt_resolution_phase_includes_action(comm_agent, context_phase2):
    _, user = comm_agent.build_prompt(context_phase2)
    assert "restart_service" in user


def test_build_prompt_resolution_phase_includes_target(comm_agent, context_phase2):
    _, user = comm_agent.build_prompt(context_phase2)
    assert "payment-api" in user


def test_build_prompt_resolution_phase_includes_execution_result(comm_agent, context_phase2):
    _, user = comm_agent.build_prompt(context_phase2)
    assert "Service restarted successfully" in user


def test_build_prompt_resolution_phase_no_ongoing_investigation(comm_agent, context_phase2):
    _, user = comm_agent.build_prompt(context_phase2)
    assert "investigation is ongoing" not in user.lower()


# ------------------------------------------------------------------ #
# System prompt                                                         #
# ------------------------------------------------------------------ #

def test_system_prompt_includes_slo(comm_agent, context_phase1):
    system, _ = comm_agent.build_prompt(context_phase1)
    assert "15%" in system


def test_system_prompt_no_contradict(comm_agent, context_phase1):
    system, _ = comm_agent.build_prompt(context_phase1)
    assert "contradict" in system.lower()


def test_system_prompt_no_premature_resolution(comm_agent, context_phase1):
    system, _ = comm_agent.build_prompt(context_phase1)
    assert "resolution" in system.lower()


def test_system_prompt_json_only(comm_agent, context_phase1):
    system, _ = comm_agent.build_prompt(context_phase1)
    assert "JSON" in system


# ------------------------------------------------------------------ #
# parse_response                                                        #
# ------------------------------------------------------------------ #

def test_parse_response_creates_updates(comm_agent, context_phase1):
    response = json.dumps({
        "updates": [
            {
                "channel": "slack-incidents",
                "update_type": "initial",
                "content": "Incident declared. Investigating payment-api degradation.",
            },
            {
                "channel": "status-page",
                "update_type": "initial",
                "content": "We are investigating reports of payment issues.",
            },
        ],
        "reasoning": "Notifying all stakeholder channels",
    })
    result = comm_agent.parse_response(response, context_phase1)
    assert isinstance(result, CommunicationResult)
    assert len(result.updates_sent) == 2
    assert all(isinstance(u, CommunicationUpdate) for u in result.updates_sent)


def test_parse_response_update_fields(comm_agent, context_phase1):
    response = json.dumps({
        "updates": [
            {
                "channel": "slack-incidents",
                "update_type": "initial",
                "content": "Investigating degradation.",
            }
        ],
        "reasoning": "Initial update sent",
    })
    result = comm_agent.parse_response(response, context_phase1)
    update = result.updates_sent[0]
    assert update.channel == "slack-incidents"
    assert update.update_type == "initial"
    assert update.content == "Investigating degradation."


def test_parse_response_timestamp_is_iso8601(comm_agent, context_phase1):
    """Timestamp must be set and look like an ISO 8601 string."""
    response = json.dumps({
        "updates": [
            {
                "channel": "slack-incidents",
                "update_type": "initial",
                "content": "Investigating.",
            }
        ],
        "reasoning": "done",
    })
    result = comm_agent.parse_response(response, context_phase1)
    ts = result.updates_sent[0].timestamp
    assert ts is not None
    assert "T" in ts or "-" in ts  # basic ISO 8601 shape


def test_parse_response_reasoning_preserved(comm_agent, context_phase1):
    response = json.dumps({
        "updates": [],
        "reasoning": "No channels configured yet",
    })
    result = comm_agent.parse_response(response, context_phase1)
    assert result.reasoning == "No channels configured yet"


def test_parse_response_empty_updates(comm_agent, context_phase1):
    response = json.dumps({
        "updates": [],
        "reasoning": "Nothing to send",
    })
    result = comm_agent.parse_response(response, context_phase1)
    assert result.updates_sent == []


def test_parse_response_markdown_fenced_json(comm_agent, context_phase1):
    """_parse_json must strip markdown fences before parsing."""
    inner = json.dumps({
        "updates": [
            {"channel": "email", "update_type": "initial", "content": "Investigating."}
        ],
        "reasoning": "ok",
    })
    response = f"```json\n{inner}\n```"
    result = comm_agent.parse_response(response, context_phase1)
    assert len(result.updates_sent) == 1


# ------------------------------------------------------------------ #
# _apply_result — append behaviour                                     #
# ------------------------------------------------------------------ #

def test_apply_result_sets_communication_when_none(comm_agent, context_phase1):
    result = CommunicationResult(
        updates_sent=[
            CommunicationUpdate(
                channel="slack",
                timestamp="2026-03-19T10:05:00Z",
                update_type="initial",
                content="Investigating.",
            )
        ],
        reasoning="Phase 1 done",
    )
    updated = comm_agent._apply_result(context_phase1, result)
    assert updated.communication is not None
    assert len(updated.communication.updates_sent) == 1


def test_apply_result_appends(comm_agent, context_phase2):
    """Second run must APPEND to existing updates, not replace them."""
    # Seed phase 1 communication
    phase1_update = CommunicationUpdate(
        channel="slack",
        timestamp="2026-03-19T10:05:00Z",
        update_type="initial",
        content="Investigating.",
    )
    context_phase2.communication = CommunicationResult(
        updates_sent=[phase1_update],
        reasoning="Phase 1 sent",
    )

    phase2_result = CommunicationResult(
        updates_sent=[
            CommunicationUpdate(
                channel="slack",
                timestamp="2026-03-19T10:20:00Z",
                update_type="resolution",
                content="Incident resolved.",
            )
        ],
        reasoning="Phase 2 sent",
    )
    updated = comm_agent._apply_result(context_phase2, phase2_result)
    # Must have BOTH updates
    assert len(updated.communication.updates_sent) == 2
    assert updated.communication.updates_sent[0] is phase1_update


def test_apply_result_updates_reasoning_on_append(comm_agent, context_phase2):
    """Reasoning is updated to the latest result's reasoning after append."""
    context_phase2.communication = CommunicationResult(
        updates_sent=[],
        reasoning="Phase 1 reasoning",
    )
    phase2_result = CommunicationResult(
        updates_sent=[],
        reasoning="Phase 2 reasoning",
    )
    updated = comm_agent._apply_result(context_phase2, phase2_result)
    assert updated.communication.reasoning == "Phase 2 reasoning"


def test_apply_result_returns_context(comm_agent, context_phase1):
    result = CommunicationResult(updates_sent=[], reasoning="ok")
    updated = comm_agent._apply_result(context_phase1, result)
    assert updated is context_phase1


# ------------------------------------------------------------------ #
# CommunicationResult defaults                                         #
# ------------------------------------------------------------------ #

def test_empty_updates_sent_default():
    result = CommunicationResult()
    assert result.updates_sent == []
    assert result.reasoning == ""


def test_communication_result_updates_are_independent():
    """Two CommunicationResult instances share no mutable default list."""
    r1 = CommunicationResult()
    r2 = CommunicationResult()
    r1.updates_sent.append(
        CommunicationUpdate(
            channel="slack", timestamp="2026-03-19T10:00:00Z",
            update_type="initial", content="x"
        )
    )
    assert len(r2.updates_sent) == 0
