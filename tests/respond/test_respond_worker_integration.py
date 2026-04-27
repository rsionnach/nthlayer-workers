"""End-to-end integration test for RespondModule (P3-E.1, step 12).

Drives a full incident through the worker module + coordinator + agent
pipeline, with stub agents producing deterministic results in lieu of real
LLM calls. Verifies the full lineage chain reaches core through every step.

This is the milestone test for P3-E.1: a seeded correlation_snapshot results
in the full triage → investigation → communication → remediation verdict
chain submitted to core, all chained back to the seeded snapshot via
parent_ids.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nthlayer_common.api_client import APIResult

from nthlayer_workers.respond.agents.base import AgentBase
from nthlayer_workers.respond.config import RespondConfig
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
from nthlayer_workers.respond.worker import RespondModule


# ------------------------------------------------------------------ #
# Stub agents — deterministic results without LLM calls               #
# ------------------------------------------------------------------ #


class _StubTriage(AgentBase):
    role = AgentRole.TRIAGE

    def build_prompt(self, ctx): return ("system", "user")
    def parse_response(self, response, ctx):
        return TriageResult(
            severity=1,
            blast_radius=["payment-api", "checkout-svc"],
            affected_slos=["availability"],
            assigned_team="payments",
            reasoning="critical service breach",
            confidence=0.85,
        )
    def _apply_result(self, ctx, result):
        ctx.triage = result
        return ctx
    async def _call_model(self, system, user):
        return "{}"  # parse_response ignores the response


class _StubInvestigation(AgentBase):
    role = AgentRole.INVESTIGATION

    def build_prompt(self, ctx): return ("system", "user")
    def parse_response(self, response, ctx):
        return InvestigationResult(
            hypotheses=[
                Hypothesis(
                    description="recent deployment caused breach",
                    confidence=0.8,
                    evidence=["deploy-v2.3.1 at T-5m"],
                    change_candidate="deploy-v2.3.1",
                )
            ],
            root_cause="deploy-v2.3.1",
            root_cause_confidence=0.8,
            reasoning="strong correlation",
            confidence=0.8,
        )
    def _apply_result(self, ctx, result):
        ctx.investigation = result
        return ctx
    async def _call_model(self, system, user):
        return "{}"


class _StubCommunication(AgentBase):
    role = AgentRole.COMMUNICATION

    def build_prompt(self, ctx): return ("system", "user")
    def parse_response(self, response, ctx):
        return CommunicationResult(
            updates_sent=[
                CommunicationUpdate(
                    channel="stdout",
                    timestamp="2026-04-25T12:00:00Z",
                    update_type="initial",
                    content="incident underway, root cause: deploy-v2.3.1",
                )
            ],
            reasoning="status update sent",
            confidence=0.9,
        )
    def _apply_result(self, ctx, result):
        ctx.communication = result
        return ctx
    async def _call_model(self, system, user):
        return "{}"


class _StubRemediation(AgentBase):
    role = AgentRole.REMEDIATION

    def __init__(self, *args, safe_action_registry=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._registry = safe_action_registry

    def build_prompt(self, ctx): return ("system", "user")
    def parse_response(self, response, ctx):
        return RemediationResult(
            proposed_action="rollback",
            target="payment-api",
            risk_assessment="low risk; rollback to last known good",
            requires_human_approval=False,
            reasoning="proposed rollback",
            confidence=0.85,
        )
    def _apply_result(self, ctx, result):
        ctx.remediation = result
        return ctx
    async def _call_model(self, system, user):
        return "{}"


# ------------------------------------------------------------------ #
# Integration test                                                     #
# ------------------------------------------------------------------ #


def _seeded_snapshot():
    return {
        "id": "asm-snap-INT-001",
        "created_at": "2026-04-25T12:00:00+00:00",
        "kind": "correlation_snapshot",
        "service": "fraud-detect",
        "data": {
            "domain": {"service": "fraud-detect", "environment": "production"},
            "parent_ids": ["vrd-breach-INT-001"],
            "affected_services": ["fraud-detect", "payment-api"],
            "blast_radius": ["payment-api", "checkout-svc"],
            "peak_severity": 0.9,
            "event_count": 7,
            "nl_summary": "fraud-detect reversal_rate exceeded 8% over 2m window",
        },
    }


async def test_p3_e1_milestone_full_chain_to_core():
    """P3-E.1 milestone: seeded correlation_snapshot drives a full pipeline,
    every verdict reaches core via submit_verdict, and parent_ids form an
    unbroken chain back to the upstream trigger.
    """
    submitted_envelopes: list[dict] = []

    async def capture_submit(payload):
        submitted_envelopes.append(payload)
        return APIResult(ok=True, status_code=200, data={}, error=None, detail=None)

    fake_client = AsyncMock()
    fake_client.get_assessments = AsyncMock(
        return_value=APIResult(ok=True, status_code=200, data=[_seeded_snapshot()],
                               error=None, detail=None)
    )
    fake_client.get_verdicts = AsyncMock(
        return_value=APIResult(ok=True, status_code=200, data=[], error=None, detail=None)
    )
    fake_client.submit_verdict = AsyncMock(side_effect=capture_submit)

    config = RespondConfig(
        cycle_interval_seconds=30.0,
        fallback_threshold_seconds=60.0,
        terminal_retention_seconds=86400.0,
        step_timeout_seconds=10.0,
    )

    module = RespondModule(client=fake_client, config=config, deployment_id="test-dep")

    # Inject stub agents bypassing LLM + prompt-file dependencies
    agent_kwargs = {
        "model": config.model,
        "max_tokens": config.max_tokens,
        "client": fake_client,
        "deployment_id": "test-dep",
        "config": {
            "root_cause_threshold": config.root_cause_threshold,
            "arbiter_url": config.arbiter_url,
            "escalation_threshold": config.escalation_threshold,
        },
    }
    module._agents = {
        AgentRole.TRIAGE: _StubTriage(**agent_kwargs),
        AgentRole.INVESTIGATION: _StubInvestigation(**agent_kwargs),
        AgentRole.COMMUNICATION: _StubCommunication(**agent_kwargs),
        AgentRole.REMEDIATION: _StubRemediation(**agent_kwargs),
    }

    # --- Cycle 1 ---
    await module.process_cycle()

    # An incident was opened from the snapshot
    assert len(module._incidents) == 1
    incident = next(iter(module._incidents.values()))

    # Pipeline drove to RESOLVED (no human approval required by stub)
    assert incident.state == IncidentState.RESOLVED, (
        f"expected RESOLVED, got {incident.state} (error: {incident.error})"
    )

    # Trigger metadata preserved
    assert incident.trigger_source == "nthlayer-correlate"
    assert "asm-snap-INT-001" in incident.trigger_verdict_ids
    assert "vrd-breach-INT-001" in incident.trigger_verdict_ids

    # All four pipeline phases completed
    assert incident.triage is not None
    assert incident.investigation is not None
    assert incident.communication is not None
    assert incident.remediation is not None

    # Pipeline produces a verdict per agent step. The 4-step pipeline runs
    # COMMUNICATION twice (initial + resolution), so 5 verdicts is the
    # minimum (triage + investigation + communication x2 + remediation).
    assert len(incident.verdict_chain) >= 5
    assert len(submitted_envelopes) == len(incident.verdict_chain)

    # --- Lineage assertions ---

    # Find the first verdict (triage) — its parent_ids must reference upstream
    submitted_by_id = {env["id"]: env for env in submitted_envelopes}
    first_verdict_id = incident.verdict_chain[0]
    first_envelope = submitted_by_id[first_verdict_id]
    first_parent_ids = first_envelope.get("parent_ids", [])
    assert "asm-snap-INT-001" in first_parent_ids, (
        f"first verdict must reference upstream snapshot, got parent_ids={first_parent_ids!r}"
    )
    assert "vrd-breach-INT-001" in first_parent_ids, (
        f"first verdict must reference upstream breach, got parent_ids={first_parent_ids!r}"
    )

    # Subsequent verdicts chain to the previous respond verdict
    for i in range(1, len(incident.verdict_chain)):
        v_id = incident.verdict_chain[i]
        prev_id = incident.verdict_chain[i - 1]
        env = submitted_by_id[v_id]
        assert env.get("parent_ids") == [prev_id], (
            f"verdict {i} ({v_id}) must reference previous {prev_id}, "
            f"got parent_ids={env.get('parent_ids')!r}"
        )

    # --- State persistence ---

    # get_state() (called by runner) returns active+recent incidents
    state = await module.get_state()
    # RESOLVED is terminal but recently updated, so it's NOT pruned (within retention)
    assert incident.id in state["incidents"]
    # Cursor advanced
    assert state["cursors"]["snapshot_after"] == "2026-04-25T12:00:00+00:00"

    # CRITICAL: verdict_chain preserved across roundtrip
    persisted_chain = state["incidents"][incident.id]["verdict_chain"]
    assert persisted_chain == list(incident.verdict_chain), (
        "verdict_chain must survive serialisation roundtrip — load-bearing for "
        "post-restart lineage continuity"
    )

    # --- Cycle 2: idempotency ---

    # Second cycle should not open a duplicate incident from the same snapshot
    await module.process_cycle()
    assert len(module._incidents) == 1, "snapshot must not re-trigger an incident"


async def test_p3_e1_fallback_path_full_chain():
    """Same end-to-end test, but via the fallback (orphan quality_breach) path.

    Validates that fallback-path lineage is structurally identical to the
    snapshot-path lineage — same chaining rules, just a different originating
    trigger and trigger_source label.
    """
    submitted_envelopes: list[dict] = []

    async def capture_submit(payload):
        submitted_envelopes.append(payload)
        return APIResult(ok=True, status_code=200, data={}, error=None, detail=None)

    fake_client = AsyncMock()
    # No snapshots at all → fallback fires
    fake_client.get_assessments = AsyncMock(
        return_value=APIResult(ok=True, status_code=200, data=[], error=None, detail=None)
    )
    # An orphan breach older than the fallback threshold
    fake_client.get_verdicts = AsyncMock(
        return_value=APIResult(
            ok=True, status_code=200,
            data=[{
                "id": "vrd-orphan-INT-002",
                "created_at": "2026-04-25T11:00:00+00:00",
                "type": "quality_breach",
                "service": "fraud-detect",
                "severity": "critical",
            }],
            error=None, detail=None,
        )
    )
    fake_client.submit_verdict = AsyncMock(side_effect=capture_submit)

    config = RespondConfig(
        cycle_interval_seconds=30.0,
        fallback_threshold_seconds=0.0,  # fire immediately
        terminal_retention_seconds=86400.0,
        step_timeout_seconds=10.0,
    )

    module = RespondModule(client=fake_client, config=config, deployment_id="test-dep")
    agent_kwargs = {
        "model": config.model,
        "max_tokens": config.max_tokens,
        "client": fake_client,
        "deployment_id": "test-dep",
        "config": {
            "root_cause_threshold": config.root_cause_threshold,
            "arbiter_url": config.arbiter_url,
            "escalation_threshold": config.escalation_threshold,
        },
    }
    module._agents = {
        AgentRole.TRIAGE: _StubTriage(**agent_kwargs),
        AgentRole.INVESTIGATION: _StubInvestigation(**agent_kwargs),
        AgentRole.COMMUNICATION: _StubCommunication(**agent_kwargs),
        AgentRole.REMEDIATION: _StubRemediation(**agent_kwargs),
    }

    await module.process_cycle()

    assert len(module._incidents) == 1
    incident = next(iter(module._incidents.values()))
    assert incident.trigger_source == "nthlayer-measure-fallback"
    assert incident.metadata["fallback_reason"] == "no_correlation_snapshot"
    assert incident.state == IncidentState.RESOLVED

    # First verdict's parent_ids points to the orphan breach
    submitted_by_id = {env["id"]: env for env in submitted_envelopes}
    first_envelope = submitted_by_id[incident.verdict_chain[0]]
    assert first_envelope.get("parent_ids") == ["vrd-orphan-INT-002"]
