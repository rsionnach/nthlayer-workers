# tests/test_types.py
"""Tests for core Mayday types."""
from nthlayer_workers.respond.types import (
    AgentRole,
    CommunicationResult,
    CommunicationUpdate,
    Hypothesis,
    IncidentContext,
    IncidentState,
    InvestigationResult,
    RemediationResult,
    TriageResult,
)


def test_incident_state_values():
    assert IncidentState.TRIGGERED == "triggered"
    assert IncidentState.AWAITING_APPROVAL == "awaiting_approval"
    assert IncidentState.ESCALATED == "escalated"
    assert IncidentState.FAILED == "failed"


def test_incident_state_terminal():
    terminal = {IncidentState.RESOLVED, IncidentState.ESCALATED, IncidentState.FAILED}
    non_terminal = {
        IncidentState.TRIGGERED,
        IncidentState.TRIAGING,
        IncidentState.INVESTIGATING,
        IncidentState.REMEDIATING,
        IncidentState.AWAITING_APPROVAL,
    }
    assert terminal & non_terminal == set()


def test_agent_role_values():
    assert AgentRole.TRIAGE == "triage"
    assert AgentRole.INVESTIGATION == "investigation"
    assert AgentRole.COMMUNICATION == "communication"
    assert AgentRole.REMEDIATION == "remediation"


def test_triage_result():
    result = TriageResult(
        severity=1,
        blast_radius=["payment-api", "checkout-service"],
        affected_slos=["availability"],
        assigned_team="payments-oncall",
        reasoning="High severity due to cascading failure",
    )
    assert result.severity == 1
    assert len(result.blast_radius) == 2


def test_hypothesis():
    h = Hypothesis(
        description="Deploy removed connection pooling",
        confidence=0.87,
        evidence=["latency spike 12m after deploy"],
        change_candidate="payment-api deploy v2.3.1",
    )
    assert h.confidence == 0.87
    assert h.change_candidate is not None


def test_investigation_result():
    result = InvestigationResult(
        hypotheses=[],
        root_cause=None,
        root_cause_confidence=0.0,
        reasoning="Insufficient evidence",
    )
    assert result.root_cause is None


def test_communication_update():
    update = CommunicationUpdate(
        channel="slack",
        timestamp="2026-03-19T10:00:00Z",
        update_type="initial",
        content="We are investigating a payment-api issue.",
    )
    assert update.update_type == "initial"


def test_communication_result_defaults():
    result = CommunicationResult()
    assert result.updates_sent == []
    assert result.reasoning == ""


def test_remediation_result_defaults():
    result = RemediationResult()
    assert result.proposed_action is None
    assert result.requires_human_approval is True
    assert result.executed is False
    assert result.autonomy_reduced is False


def test_incident_context_minimal():
    ctx = IncidentContext(
        id="INC-2026-0001",
        state=IncidentState.TRIGGERED,
        created_at="2026-03-19T10:00:00Z",
        updated_at="2026-03-19T10:00:00Z",
        trigger_source="nthlayer-correlate",
        trigger_verdict_ids=["vrd-2026-03-19-abc12345-00001"],
        topology={},
    )
    assert ctx.triage is None
    assert ctx.verdict_chain == []
    assert ctx.last_completed_step_index is None
    assert ctx.error is None


def test_incident_context_with_results():
    ctx = IncidentContext(
        id="INC-2026-0001",
        state=IncidentState.INVESTIGATING,
        created_at="2026-03-19T10:00:00Z",
        updated_at="2026-03-19T10:05:00Z",
        trigger_source="nthlayer-correlate",
        trigger_verdict_ids=[],
        topology={},
        triage=TriageResult(
            severity=1,
            blast_radius=["payment-api"],
            affected_slos=["availability"],
            assigned_team="payments",
            reasoning="test",
        ),
        verdict_chain=["vrd-001", "vrd-002"],
        last_completed_step_index=0,
    )
    assert ctx.triage is not None
    assert ctx.last_completed_step_index == 0
    assert len(ctx.verdict_chain) == 2
