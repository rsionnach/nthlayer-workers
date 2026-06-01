# tests/test_remediation.py
"""Tests for RemediationAgent."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from nthlayer_common.verdicts import MemoryStore

from nthlayer_workers.respond.agents.remediation import RemediationAgent
from nthlayer_workers.respond.safe_actions.actions import register_builtin_actions
from nthlayer_workers.respond.safe_actions.registry import SafeActionRegistry
from nthlayer_workers.respond.types import (
    AgentRole,
    Hypothesis,
    IncidentContext,
    IncidentState,
    InvestigationResult,
    RemediationResult,
    TriageResult,
)

# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #

def make_registry(tmp_path, *, with_builtins: bool = True) -> SafeActionRegistry:
    registry = SafeActionRegistry(cooldown_store_path=str(tmp_path / "cooldown.db"))
    if with_builtins:
        register_builtin_actions(registry)
    return registry


# ------------------------------------------------------------------ #
# Fixtures                                                             #
# ------------------------------------------------------------------ #

@pytest.fixture
def verdict_store():
    return MemoryStore()


@pytest.fixture
def registry(tmp_path):
    return make_registry(tmp_path)


@pytest.fixture
def agent(verdict_store, registry):
    return RemediationAgent(
        model="test-model",
        max_tokens=256,
        verdict_store=verdict_store,
        config={"arbiter_url": "http://arbiter.local"},
        safe_action_registry=registry,
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
        hypotheses=[
            Hypothesis(
                description="Bad deploy introduced regression",
                confidence=0.85,
                evidence=["error_rate_spike", "deploy_timing"],
                change_candidate="deploy-20260319-001",
            )
        ],
        root_cause="Bad deploy introduced regression in payment-api",
        root_cause_confidence=0.85,
        reasoning="Strong temporal correlation with recent deploy",
    )


@pytest.fixture
def context(verdict_store, triage_result, investigation_result):
    ctx = IncidentContext(
        id="INC-2026-0001",
        state=IncidentState.REMEDIATING,
        created_at="2026-03-19T10:00:00Z",
        updated_at="2026-03-19T10:10:00Z",
        trigger_source="nthlayer-correlate",
        trigger_verdict_ids=[],
        topology={
            "services": [
                {"name": "payment-api", "tier": "critical", "dependencies": ["database-primary"]},
                {"name": "checkout-service", "tier": "critical", "dependencies": ["payment-api"]},
            ]
        },
    )
    ctx.triage = triage_result
    ctx.investigation = investigation_result
    return ctx


# ------------------------------------------------------------------ #
# Role and timeout                                                     #
# ------------------------------------------------------------------ #

def test_role_and_timeout(agent):
    assert agent.role == AgentRole.REMEDIATION
    assert agent.default_timeout == 30


# ------------------------------------------------------------------ #
# build_prompt                                                          #
# ------------------------------------------------------------------ #

def test_build_prompt_includes_safe_actions(agent, context):
    system, user = agent.build_prompt(context)
    # All five built-in action names must appear in the system prompt
    for name in ("rollback", "scale_up", "disable_feature_flag", "reduce_autonomy", "pause_pipeline"):
        assert name in system, f"Expected safe action {name!r} in system prompt"


def test_system_prompt_includes_slo(agent, context):
    system, _ = agent.build_prompt(context)
    assert "80%" in system


def test_system_prompt_prohibits_novel_actions(agent, context):
    system, _ = agent.build_prompt(context)
    assert "ONLY" in system or "only" in system or "MUST NOT" in system


def test_build_prompt_includes_root_cause(agent, context):
    _, user = agent.build_prompt(context)
    assert "payment-api" in user or "regression" in user.lower()


def test_build_prompt_includes_triage_severity(agent, context):
    _, user = agent.build_prompt(context)
    assert "1" in user  # severity


def test_build_prompt_includes_topology(agent, context):
    _, user = agent.build_prompt(context)
    assert "payment-api" in user


def test_build_prompt_no_investigation(agent, context):
    """Should not raise when investigation is None."""
    context.investigation = None
    system, user = agent.build_prompt(context)
    assert "remediation" in system.lower()


def test_build_prompt_no_triage(agent, context):
    """Should not raise when triage is None."""
    context.triage = None
    system, user = agent.build_prompt(context)
    assert "remediation" in system.lower()


# ------------------------------------------------------------------ #
# parse_response — valid action                                        #
# ------------------------------------------------------------------ #

def test_parse_response_valid_action(agent, context):
    response = json.dumps({
        "proposed_action": "rollback",
        "target": "payment-api",
        "risk_assessment": "Low risk — rollback to last stable deploy",
        "requires_human_approval": True,
        "autonomy_reduction": {"recommended": False},
        "reasoning": "Deploy timing matches incident onset exactly.",
    })
    result = agent.parse_response(response, context)
    assert isinstance(result, RemediationResult)
    assert result.proposed_action == "rollback"
    assert result.target == "payment-api"
    assert result.risk_assessment == "Low risk — rollback to last stable deploy"
    assert result.reasoning == "Deploy timing matches incident onset exactly."


def test_parse_response_scale_up_no_approval_required(agent, context):
    """scale_up has requires_approval=False, model says False → stays False."""
    response = json.dumps({
        "proposed_action": "scale_up",
        "target": "payment-api",
        "risk_assessment": "Minimal risk",
        "requires_human_approval": False,
        "autonomy_reduction": {"recommended": False},
        "reasoning": "Capacity exhaustion evident.",
    })
    result = agent.parse_response(response, context)
    assert result.proposed_action == "scale_up"
    assert result.requires_human_approval is False


# ------------------------------------------------------------------ #
# parse_response — hallucinated action                                 #
# ------------------------------------------------------------------ #

def test_parse_response_hallucinated_action(agent, context):
    """Action NOT in registry → proposed_action=None, requires_human_approval=True."""
    response = json.dumps({
        "proposed_action": "restart_kubernetes_cluster",
        "target": "payment-api",
        "risk_assessment": "Drastic",
        "requires_human_approval": False,
        "autonomy_reduction": {"recommended": False},
        "reasoning": "Nuclear option.",
    })
    result = agent.parse_response(response, context)
    assert result.proposed_action is None
    assert result.requires_human_approval is True


def test_parse_response_hallucinated_action_logs_warning(agent, context):
    """Hallucinated action should produce a log warning."""
    response = json.dumps({
        "proposed_action": "delete_database",
        "target": "db-primary",
        "risk_assessment": "Catastrophic",
        "requires_human_approval": False,
        "autonomy_reduction": {"recommended": False},
        "reasoning": "Wipe it.",
    })
    with patch("nthlayer_workers.respond.agents.remediation.logger") as mock_logger:
        result = agent.parse_response(response, context)
    mock_logger.warning.assert_called_once()
    assert result.proposed_action is None


# ------------------------------------------------------------------ #
# parse_response — approval ratchet                                    #
# ------------------------------------------------------------------ #

def test_approval_ratchet(agent, context):
    """Registry action.requires_approval=True, model says False → forced to True."""
    # rollback has requires_approval=True in builtins
    response = json.dumps({
        "proposed_action": "rollback",
        "target": "payment-api",
        "risk_assessment": "Low",
        "requires_human_approval": False,  # model tries to downgrade — must be overridden
        "autonomy_reduction": {"recommended": False},
        "reasoning": "Auto-rollback seems fine.",
    })
    result = agent.parse_response(response, context)
    assert result.requires_human_approval is True


def test_approval_no_ratchet_when_model_escalates(agent, context):
    """Model says True, registry action.requires_approval=False → True is kept (model can escalate)."""
    # scale_up has requires_approval=False in builtins
    response = json.dumps({
        "proposed_action": "scale_up",
        "target": "payment-api",
        "risk_assessment": "Medium",
        "requires_human_approval": True,  # model escalates — this is allowed
        "autonomy_reduction": {"recommended": False},
        "reasoning": "Being cautious.",
    })
    result = agent.parse_response(response, context)
    assert result.requires_human_approval is True


def test_approval_ratchet_pause_pipeline(agent, context):
    """pause_pipeline requires_approval=True → model False is overridden."""
    response = json.dumps({
        "proposed_action": "pause_pipeline",
        "target": "payment-pipeline",
        "risk_assessment": "Medium",
        "requires_human_approval": False,
        "autonomy_reduction": {"recommended": False},
        "reasoning": "Stop further propagation.",
    })
    result = agent.parse_response(response, context)
    assert result.requires_human_approval is True


def test_approval_ratchet_disable_feature_flag(agent, context):
    """disable_feature_flag requires_approval=True → model False is overridden."""
    response = json.dumps({
        "proposed_action": "disable_feature_flag",
        "target": "new-checkout-flow",
        "risk_assessment": "Low",
        "requires_human_approval": False,
        "autonomy_reduction": {"recommended": False},
        "reasoning": "Disable the new flag.",
    })
    result = agent.parse_response(response, context)
    assert result.requires_human_approval is True


# ------------------------------------------------------------------ #
# parse_response — autonomy reduction field                            #
# ------------------------------------------------------------------ #

def test_parse_response_autonomy_reduction_not_recommended(agent, context):
    response = json.dumps({
        "proposed_action": "scale_up",
        "target": "payment-api",
        "risk_assessment": "Low",
        "requires_human_approval": False,
        "autonomy_reduction": {"recommended": False},
        "reasoning": "Scaling is safe.",
    })
    result = agent.parse_response(response, context)
    # No autonomy reduction — the raw model data is stored for _post_execute
    assert result.proposed_action == "scale_up"


def test_parse_response_missing_keys_defaults(agent, context):
    """Missing optional keys should not raise."""
    response = json.dumps({
        "proposed_action": "scale_up",
        "target": "payment-api",
    })
    result = agent.parse_response(response, context)
    assert result.proposed_action == "scale_up"
    assert result.requires_human_approval is True  # safe default when missing from JSON


# ------------------------------------------------------------------ #
# _post_execute — safe action execution                                #
# ------------------------------------------------------------------ #

@pytest.mark.asyncio
async def test_post_execute_runs_safe_action(agent, context):
    """When requires_human_approval=False, registry.execute() is called."""
    result = RemediationResult(
        proposed_action="scale_up",
        target="payment-api",
        risk_assessment="Low",
        requires_human_approval=False,
        reasoning="Safe to auto-execute.",
    )

    mock_exec = AsyncMock(return_value={"success": True, "detail": "scaled up", "timestamp": "2026-03-19T10:10:00Z"})
    agent._registry.execute = mock_exec

    await agent._post_execute(context, result)

    mock_exec.assert_called_once_with("scale_up", "payment-api", context)
    assert result.executed is True
    assert result.execution_result == "scaled up"


@pytest.mark.asyncio
async def test_post_execute_skips_when_approval_required(agent, context):
    """When requires_human_approval=True, registry.execute() is NOT called."""
    result = RemediationResult(
        proposed_action="rollback",
        target="payment-api",
        risk_assessment="Low",
        requires_human_approval=True,
        reasoning="Needs human sign-off.",
    )

    mock_exec = AsyncMock(return_value={"success": True, "detail": "rolled back", "timestamp": "2026-03-19T10:10:00Z"})
    agent._registry.execute = mock_exec

    await agent._post_execute(context, result)

    mock_exec.assert_not_called()
    assert result.executed is False


@pytest.mark.asyncio
async def test_post_execute_skips_when_proposed_action_none(agent, context):
    """When proposed_action is None (hallucinated), registry.execute() is NOT called."""
    result = RemediationResult(
        proposed_action=None,
        target="payment-api",
        risk_assessment="Unknown",
        requires_human_approval=True,
        reasoning="Hallucinated action blocked.",
    )

    mock_exec = AsyncMock()
    agent._registry.execute = mock_exec

    await agent._post_execute(context, result)

    mock_exec.assert_not_called()
    assert result.executed is False


@pytest.mark.asyncio
async def test_post_execute_execution_failure(agent, context):
    """When registry.execute() raises, executed=False and result captures the error."""
    result = RemediationResult(
        proposed_action="scale_up",
        target="payment-api",
        risk_assessment="Low",
        requires_human_approval=False,
        reasoning="Safe auto-execute.",
    )

    mock_exec = AsyncMock(side_effect=RuntimeError("cooldown not elapsed"))
    agent._registry.execute = mock_exec

    await agent._post_execute(context, result)

    assert result.executed is False
    assert "cooldown not elapsed" in (result.execution_result or "")


# ------------------------------------------------------------------ #
# _post_execute — autonomy reduction                                   #
# ------------------------------------------------------------------ #

@pytest.mark.asyncio
async def test_post_execute_autonomy_reduction(agent, context):
    """When autonomy_reduction is recommended, _request_autonomy_reduction is called."""
    result = RemediationResult(
        proposed_action="scale_up",
        target="payment-api",
        risk_assessment="Low",
        requires_human_approval=False,
        reasoning="Execute and reduce autonomy.",
    )
    # Inject autonomy_reduction metadata (as _post_execute expects it stored by parse_response)
    result.autonomy_reduction = {
        "recommended": True,
        "target_agent": "triage-agent",
        "arbiter_url": "http://arbiter.local",
        "reason": "Multiple misclassifications detected",
    }

    mock_exec = AsyncMock(return_value={"success": True, "detail": "ok", "timestamp": "t"})
    agent._registry.execute = mock_exec

    mock_autonomy = AsyncMock(return_value={
        "previous_level": "high",
        "new_level": "medium",
    })
    agent._request_autonomy_reduction = mock_autonomy

    await agent._post_execute(context, result)

    mock_autonomy.assert_called_once_with(
        "triage-agent",
        "http://arbiter.local",
        "Multiple misclassifications detected",
    )
    assert result.autonomy_reduced is True
    assert result.autonomy_target == "triage-agent"


@pytest.mark.asyncio
async def test_post_execute_no_autonomy_reduction_when_not_recommended(agent, context):
    """When autonomy_reduction.recommended=False, _request_autonomy_reduction is NOT called."""
    result = RemediationResult(
        proposed_action="scale_up",
        target="payment-api",
        risk_assessment="Low",
        requires_human_approval=False,
        reasoning="No autonomy reduction needed.",
    )
    result.autonomy_reduction = {"recommended": False}

    mock_exec = AsyncMock(return_value={"success": True, "detail": "ok", "timestamp": "t"})
    agent._registry.execute = mock_exec

    mock_autonomy = AsyncMock()
    agent._request_autonomy_reduction = mock_autonomy

    await agent._post_execute(context, result)

    mock_autonomy.assert_not_called()
    assert result.autonomy_reduced is False


# ------------------------------------------------------------------ #
# _post_execute — ordering: safe action BEFORE autonomy reduction     #
# ------------------------------------------------------------------ #

@pytest.mark.asyncio
async def test_post_execute_ordering(agent, context):
    """Safe action executes BEFORE autonomy reduction."""
    call_order: list[str] = []

    result = RemediationResult(
        proposed_action="scale_up",
        target="payment-api",
        risk_assessment="Low",
        requires_human_approval=False,
        reasoning="Execute both.",
    )
    result.autonomy_reduction = {
        "recommended": True,
        "target_agent": "investigation-agent",
        "arbiter_url": "http://arbiter.local",
        "reason": "Drift detected",
    }

    async def fake_execute(name, target, ctx):
        call_order.append("execute")
        return {"success": True, "detail": "done", "timestamp": "t"}

    async def fake_autonomy(agent_name, url, reason):
        call_order.append("autonomy_reduction")
        return {"previous_level": "high", "new_level": "low"}

    agent._registry.execute = fake_execute
    agent._request_autonomy_reduction = fake_autonomy

    await agent._post_execute(context, result)

    assert call_order == ["execute", "autonomy_reduction"]


# ------------------------------------------------------------------ #
# _apply_result                                                         #
# ------------------------------------------------------------------ #

def test_apply_result_sets_remediation(agent, context):
    result = RemediationResult(
        proposed_action="rollback",
        target="payment-api",
        risk_assessment="Low",
        requires_human_approval=True,
        reasoning="Safe rollback.",
    )
    updated = agent._apply_result(context, result)
    assert updated.remediation is result


def test_apply_result_returns_context(agent, context):
    result = RemediationResult(
        proposed_action="scale_up",
        target="payment-api",
        risk_assessment="Minimal",
        requires_human_approval=False,
        reasoning="Scale up safely.",
    )
    updated = agent._apply_result(context, result)
    assert updated is context


# ------------------------------------------------------------------ #
# Bead 1: structured remediation emission                             #
# ------------------------------------------------------------------ #

@pytest.fixture
def bare_agent(verdict_store, tmp_path):
    """Minimal RemediationAgent with an empty SafeActionRegistry — bypasses
    safe-action policy YAML loading so we can exercise pure metadata-builder
    methods (`_build_metadata`, `_build_degraded_metadata`) without setting
    up the full agent harness. The standard `agent` fixture loads
    `register_builtin_actions(registry)` which reads
    `src/registry/safe-actions.yaml` — that path doesn't exist in this tree
    (pre-existing infrastructure rot), so tests that don't need the registry
    use this slimmer fixture instead."""
    return RemediationAgent(
        model="test-model",
        max_tokens=256,
        verdict_store=verdict_store,
        config={"arbiter_url": "http://arbiter.local"},
        safe_action_registry=make_registry(tmp_path, with_builtins=False),
    )


def test_build_metadata_carries_proposed_action_and_target(bare_agent):
    """Normal-path remediation verdicts carry proposed_action and target
    in metadata.custom — structured fields for bench brief and other
    downstream consumers (Bead 1)."""
    result = RemediationResult(
        proposed_action="rollback",
        target="payment-api",
        risk_assessment="Low",
        requires_human_approval=True,
        reasoning="Safe rollback.",
    )
    md = bare_agent._build_metadata(result)
    assert md == {"custom": {"proposed_action": "rollback", "target": "payment-api"}}


def test_build_metadata_with_rejected_hallucinated_action(bare_agent):
    """When parse_response rejects a hallucinated action it sets
    proposed_action=None but leaves target intact. Metadata must reflect
    that exactly so consumers can disambiguate 'no proposal' from
    'rejected proposal'."""
    result = RemediationResult(
        proposed_action=None,            # rejected by registry
        target="payment-api",            # model still suggested a target
        risk_assessment="",
        requires_human_approval=True,
        reasoning="hallucinated action rejected",
    )
    md = bare_agent._build_metadata(result)
    assert md == {"custom": {"proposed_action": None, "target": "payment-api"}}


def test_build_metadata_preserves_empty_strings(bare_agent):
    """Empty-string fields are propagated as-is, not coerced to None.
    Consumers (bench brief) see what the model actually produced and can
    decide how to render — coercing to None here would lose information."""
    result = RemediationResult(
        proposed_action="rollback",
        target="",                       # model omitted target
        risk_assessment="",
        requires_human_approval=True,
        reasoning="rollback recommended; target unspecified",
    )
    md = bare_agent._build_metadata(result)
    assert md == {"custom": {"proposed_action": "rollback", "target": ""}}


def test_degraded_metadata_survives_http_round_trip_to_bench(bare_agent):
    """Bench reads remediation verdicts from core's HTTP API as JSON dicts.
    The metadata.custom shape — including None values — must survive a
    to_dict/from_dict round-trip so bench brief sees the same data the worker
    emitted."""
    from nthlayer_common.verdicts import create as verdict_create
    from nthlayer_common.verdicts.serialise import from_dict, to_dict

    md = bare_agent._build_degraded_metadata()
    v = verdict_create(
        subject={"type": "remediation", "ref": "INC-TEST", "summary": "degraded"},
        judgment={"action": "escalate", "confidence": 0.0, "reasoning": "r"},
        producer={"system": "nthlayer-respond", "instance": "test"},
        metadata=md,
    )
    payload = to_dict(v)
    restored = from_dict(payload)
    assert restored.metadata.custom == {"proposed_action": None, "target": None}


def test_build_degraded_metadata_explicit_nones(bare_agent):
    """Degraded path: model never returned a result. Both fields explicitly
    None — distinguishable from 'non-remediation verdict' (which has no
    proposed_action key at all)."""
    md = bare_agent._build_degraded_metadata()
    assert md == {"custom": {"proposed_action": None, "target": None}}


# ------------------------------------------------------------------ #
# None-registry safety (opensrm-saun.1.3)                              #
# ------------------------------------------------------------------ #

# Worker mode currently constructs RemediationAgent with
# safe_action_registry=None pending opensrm P3-E.3 (registry wiring). The
# agent must not crash with AttributeError when parse_response runs against
# a model response that proposes a real action — instead it should preserve
# the proposal and force requires_human_approval=True since the action
# can't be verified against the policy.

@pytest.fixture
def agent_no_registry(verdict_store):
    """RemediationAgent constructed with safe_action_registry=None — mirrors
    worker.py's pre-P3-E.3 wiring."""
    return RemediationAgent(
        model="test-model",
        max_tokens=256,
        verdict_store=verdict_store,
        config={"arbiter_url": "http://arbiter.local"},
        safe_action_registry=None,
    )


def test_parse_response_no_registry_does_not_crash(agent_no_registry, context):
    """The reproduction case from the integration test: canned LLM response
    proposes a real action (rollback), but registry is None. Before the
    fix, this raised AttributeError on self._registry.get(...). After the
    fix, it returns a result with the proposal preserved and approval
    forced."""
    response = json.dumps({
        "proposed_action": "rollback",
        "target": "fraud-detect",
        "risk_assessment": "Stub remediation",
        "requires_human_approval": False,  # Model says no — we override.
        "reasoning": "test",
        "confidence": 0.85,
    })
    result = agent_no_registry.parse_response(response, context)
    assert result.proposed_action == "rollback"
    assert result.target == "fraud-detect"
    assert result.requires_human_approval is True


def test_parse_response_no_registry_logs_warning(agent_no_registry, context):
    response = json.dumps({
        "proposed_action": "rollback",
        "target": "fraud-detect",
        "requires_human_approval": True,
        "confidence": 0.85,
    })
    with patch("nthlayer_workers.respond.agents.remediation.logger") as mock_logger:
        agent_no_registry.parse_response(response, context)
    mock_logger.warning.assert_called_once()
    args, _ = mock_logger.warning.call_args
    assert "no safe-action registry" in args[0]


def test_parse_response_no_registry_no_action_proposed(agent_no_registry, context):
    """Model returned no action — registry-None path is not triggered, but
    confirm we still produce a clean result without crashing."""
    response = json.dumps({
        "proposed_action": None,
        "target": None,
        "reasoning": "no remediation available",
        "confidence": 0.5,
    })
    result = agent_no_registry.parse_response(response, context)
    assert result.proposed_action is None
    assert result.target is None


@pytest.mark.asyncio
async def test_post_execute_no_registry_skips_execution(agent_no_registry, context):
    """Defensive belt: even if requires_human_approval ended up False
    despite the registry being None (e.g. a future code path), the
    post-execute guard skips the registry.execute() call rather than
    crashing. We construct the result directly to bypass parse_response's
    approval forcing."""
    result = RemediationResult(
        proposed_action="rollback",
        target="fraud-detect",
        risk_assessment="",
        requires_human_approval=False,  # Bypassing parse_response's forcing.
        reasoning="",
        confidence=0.85,
    )
    result.autonomy_reduction = {}

    out_context = await agent_no_registry._post_execute(context, result)

    # No exception, no execution attempt logged on result.
    assert getattr(result, "executed", None) in (None, False)
    # Context returned unchanged (no autonomy_reduction either).
    assert out_context is context
