# tests/test_investigation.py
"""Tests for InvestigationAgent."""
from __future__ import annotations

import json

import pytest
from nthlayer_common.verdicts import MemoryStore
from nthlayer_common.verdicts import create as verdict_create

from nthlayer_workers.respond.agents.investigation import InvestigationAgent
from nthlayer_workers.respond.types import (
    AgentRole,
    Hypothesis,
    IncidentContext,
    IncidentState,
    InvestigationResult,
    TriageResult,
)

# ------------------------------------------------------------------ #
# Fixtures                                                             #
# ------------------------------------------------------------------ #

@pytest.fixture
def verdict_store():
    return MemoryStore()


@pytest.fixture
def investigation_agent(verdict_store):
    return InvestigationAgent(
        model="test-model",
        max_tokens=200,
        verdict_store=verdict_store,
        config={"root_cause_threshold": 0.7},
    )


@pytest.fixture
def investigation_agent_high_threshold(verdict_store):
    return InvestigationAgent(
        model="test-model",
        max_tokens=200,
        verdict_store=verdict_store,
        config={"root_cause_threshold": 0.9},
    )


@pytest.fixture
def investigation_agent_default_threshold(verdict_store):
    """Agent with no root_cause_threshold in config — should default to 0.7."""
    return InvestigationAgent(
        model="test-model",
        max_tokens=200,
        verdict_store=verdict_store,
        config={},
    )


@pytest.fixture
def sitrep_verdict(verdict_store):
    v = verdict_create(
        subject={
            "type": "correlation",
            "service": "payment-api",
            "ref": "cg-001",
            "summary": "Latency spike correlated with deploy",
        },
        judgment={
            "action": "flag",
            "confidence": 0.85,
            "reasoning": "Strong temporal correlation with recent deploy",
        },
        producer={"system": "nthlayer-correlate"},
    )
    verdict_store.put(v)
    return v


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
def context_with_triage(verdict_store, sitrep_verdict, triage_result):
    ctx = IncidentContext(
        id="INC-2026-0001",
        state=IncidentState.INVESTIGATING,
        created_at="2026-03-19T10:00:00Z",
        updated_at="2026-03-19T10:05:00Z",
        trigger_source="nthlayer-correlate",
        trigger_verdict_ids=[sitrep_verdict.id],
        topology={
            "services": [
                {"name": "payment-api", "tier": "critical", "dependencies": ["database-primary"]},
                {"name": "checkout-service", "tier": "critical", "dependencies": ["payment-api"]},
            ]
        },
    )
    ctx.triage = triage_result
    return ctx


@pytest.fixture
def context_no_triage(verdict_store):
    return IncidentContext(
        id="INC-2026-0002",
        state=IncidentState.INVESTIGATING,
        created_at="2026-03-19T10:00:00Z",
        updated_at="2026-03-19T10:05:00Z",
        trigger_source="pagerduty",
        trigger_verdict_ids=[],
        topology={"services": [{"name": "auth-service"}]},
    )


# ------------------------------------------------------------------ #
# Role and timeout                                                     #
# ------------------------------------------------------------------ #

def test_role_and_timeout(investigation_agent):
    assert investigation_agent.role == AgentRole.INVESTIGATION
    assert investigation_agent.default_timeout == 60


# ------------------------------------------------------------------ #
# build_prompt                                                          #
# ------------------------------------------------------------------ #

def test_build_prompt_includes_triage(investigation_agent, context_with_triage):
    system, user = investigation_agent.build_prompt(context_with_triage)
    # Severity and blast radius from triage result must appear in the user prompt
    assert "1" in user  # severity
    assert "payment-api" in user
    assert "checkout-service" in user


def test_build_prompt_includes_triage_affected_slos(investigation_agent, context_with_triage):
    _, user = investigation_agent.build_prompt(context_with_triage)
    assert "availability" in user or "latency_p99" in user


def test_build_prompt_includes_threshold(investigation_agent, context_with_triage):
    system, _ = investigation_agent.build_prompt(context_with_triage)
    assert "0.7" in system


def test_build_prompt_includes_topology(investigation_agent, context_with_triage):
    _, user = investigation_agent.build_prompt(context_with_triage)
    assert "payment-api" in user


def test_build_prompt_includes_trigger_verdict_info(investigation_agent, context_with_triage):
    _, user = investigation_agent.build_prompt(context_with_triage)
    # The nthlayer-correlate verdict correlation info should appear
    assert "nthlayer-correlate" in user.lower() or "correlation" in user.lower() or "latency spike" in user.lower()


def test_build_prompt_no_triage(investigation_agent, context_no_triage):
    # Should not raise even when context.triage is None
    system, user = investigation_agent.build_prompt(context_no_triage)
    assert "investigation" in system.lower()


def test_system_prompt_includes_slo(investigation_agent, context_with_triage):
    system, _ = investigation_agent.build_prompt(context_with_triage)
    assert "70%" in system


def test_build_prompt_threshold_from_config(investigation_agent_high_threshold, context_with_triage):
    system, _ = investigation_agent_high_threshold.build_prompt(context_with_triage)
    assert "0.9" in system


def test_build_prompt_default_threshold(investigation_agent_default_threshold, context_with_triage):
    system, _ = investigation_agent_default_threshold.build_prompt(context_with_triage)
    assert "0.7" in system


# ------------------------------------------------------------------ #
# parse_response                                                        #
# ------------------------------------------------------------------ #

def test_parse_response_with_root_cause(investigation_agent, context_with_triage):
    """Confidence >= threshold → root_cause preserved."""
    response = json.dumps({
        "hypotheses": [
            {
                "description": "Database connection pool exhaustion",
                "confidence": 0.85,
                "evidence": ["connection_timeout_spike", "db_pool_metrics"],
                "change_candidate": "deploy-20260319-001",
            }
        ],
        "root_cause": "Database connection pool exhausted after deploy",
        "root_cause_confidence": 0.85,
        "reasoning": "Strong evidence from metrics and recent deploy correlation",
    })
    result = investigation_agent.parse_response(response, context_with_triage)
    assert isinstance(result, InvestigationResult)
    assert result.root_cause == "Database connection pool exhausted after deploy"
    assert result.root_cause_confidence == 0.85


def test_parse_response_clears_root_cause_below_threshold(investigation_agent, context_with_triage):
    """Confidence < threshold → root_cause cleared to None mechanically."""
    response = json.dumps({
        "hypotheses": [
            {
                "description": "Memory leak in payment service",
                "confidence": 0.6,
                "evidence": ["memory_trend"],
                "change_candidate": None,
            }
        ],
        "root_cause": "Memory leak",
        "root_cause_confidence": 0.6,
        "reasoning": "Possible but not confirmed",
    })
    result = investigation_agent.parse_response(response, context_with_triage)
    assert result.root_cause is None
    # confidence value is preserved even though root_cause is cleared
    assert result.root_cause_confidence == 0.6


def test_parse_response_no_root_cause(investigation_agent, context_with_triage):
    """Model returns null root_cause → stays None."""
    response = json.dumps({
        "hypotheses": [
            {
                "description": "Network partition",
                "confidence": 0.5,
                "evidence": ["packet_loss"],
                "change_candidate": None,
            },
            {
                "description": "Dependency timeout",
                "confidence": 0.4,
                "evidence": ["timeout_logs"],
                "change_candidate": None,
            },
        ],
        "root_cause": None,
        "root_cause_confidence": 0.0,
        "reasoning": "Multiple plausible causes, insufficient evidence to declare root cause",
    })
    result = investigation_agent.parse_response(response, context_with_triage)
    assert result.root_cause is None
    assert len(result.hypotheses) == 2


def test_hypotheses_parsed(investigation_agent, context_with_triage):
    """Multiple hypotheses with change_candidates are parsed correctly."""
    response = json.dumps({
        "hypotheses": [
            {
                "description": "Deploy introduced regression",
                "confidence": 0.75,
                "evidence": ["error_rate_spike", "deploy_timing"],
                "change_candidate": "deploy-20260319-001",
            },
            {
                "description": "Traffic spike overwhelmed capacity",
                "confidence": 0.45,
                "evidence": ["request_rate_increase"],
                "change_candidate": None,
            },
            {
                "description": "Certificate rotation failure",
                "confidence": 0.3,
                "evidence": ["tls_errors"],
                "change_candidate": "cert-rotation-20260319",
            },
        ],
        "root_cause": "Deploy introduced regression",
        "root_cause_confidence": 0.75,
        "reasoning": "Deploy timing matches incident onset precisely",
    })
    result = investigation_agent.parse_response(response, context_with_triage)
    assert len(result.hypotheses) == 3

    h0 = result.hypotheses[0]
    assert isinstance(h0, Hypothesis)
    assert h0.description == "Deploy introduced regression"
    assert h0.confidence == 0.75
    assert "error_rate_spike" in h0.evidence
    assert h0.change_candidate == "deploy-20260319-001"

    h1 = result.hypotheses[1]
    assert h1.change_candidate is None

    h2 = result.hypotheses[2]
    assert h2.change_candidate == "cert-rotation-20260319"


def test_parse_response_empty_hypotheses(investigation_agent, context_with_triage):
    response = json.dumps({
        "hypotheses": [],
        "root_cause": None,
        "root_cause_confidence": 0.0,
        "reasoning": "Insufficient data",
    })
    result = investigation_agent.parse_response(response, context_with_triage)
    assert result.hypotheses == []
    assert result.root_cause is None


def test_parse_response_missing_evidence_defaults_to_list(investigation_agent, context_with_triage):
    """evidence field missing from a hypothesis → defaults to empty list."""
    response = json.dumps({
        "hypotheses": [
            {
                "description": "Unknown cause",
                "confidence": 0.2,
                # no evidence key
                "change_candidate": None,
            }
        ],
        "root_cause": None,
        "root_cause_confidence": 0.0,
        "reasoning": "Not enough signal",
    })
    result = investigation_agent.parse_response(response, context_with_triage)
    assert isinstance(result.hypotheses[0].evidence, list)


def test_parse_response_exactly_at_threshold(investigation_agent, context_with_triage):
    """Confidence exactly equal to threshold → root_cause preserved (>= comparison)."""
    response = json.dumps({
        "hypotheses": [
            {
                "description": "Exactly at threshold",
                "confidence": 0.7,
                "evidence": [],
                "change_candidate": None,
            }
        ],
        "root_cause": "Exactly at threshold cause",
        "root_cause_confidence": 0.7,
        "reasoning": "Borderline case",
    })
    result = investigation_agent.parse_response(response, context_with_triage)
    assert result.root_cause == "Exactly at threshold cause"


def test_parse_response_reasoning_preserved(investigation_agent, context_with_triage):
    response = json.dumps({
        "hypotheses": [],
        "root_cause": None,
        "root_cause_confidence": 0.0,
        "reasoning": "Detailed reasoning text here",
    })
    result = investigation_agent.parse_response(response, context_with_triage)
    assert result.reasoning == "Detailed reasoning text here"


# ------------------------------------------------------------------ #
# _apply_result                                                         #
# ------------------------------------------------------------------ #

def test_apply_result_sets_investigation(investigation_agent, context_with_triage):
    result = InvestigationResult(
        hypotheses=[],
        root_cause=None,
        root_cause_confidence=0.0,
        reasoning="No root cause found",
    )
    updated = investigation_agent._apply_result(context_with_triage, result)
    assert updated.investigation is result


def test_apply_result_returns_context(investigation_agent, context_with_triage):
    result = InvestigationResult(
        hypotheses=[],
        root_cause="Some cause",
        root_cause_confidence=0.8,
        reasoning="Found it",
    )
    updated = investigation_agent._apply_result(context_with_triage, result)
    assert updated is context_with_triage
