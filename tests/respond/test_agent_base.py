# tests/test_agent_base.py
"""Tests for AgentBase ABC."""
import json
from unittest.mock import AsyncMock, patch

import pytest

from nthlayer_workers.respond.agents.base import AgentBase
from nthlayer_workers.respond.types import (
    AgentRole,
    IncidentContext,
    IncidentState,
    TriageResult,
)


class StubAgent(AgentBase):
    """Minimal concrete agent for testing the base class."""
    role = AgentRole.TRIAGE
    default_timeout = 5

    def build_prompt(self, context):
        return ("You are a test agent.", "Assess this incident.")

    def parse_response(self, response, context):
        data = self._parse_json(response)
        return TriageResult(
            severity=data.get("severity", 2),
            blast_radius=data.get("blast_radius", []),
            affected_slos=[],
            assigned_team=None,
            reasoning=data.get("reasoning", ""),
        )

    def _apply_result(self, context, result):
        context.triage = result
        return context


@pytest.fixture
def stub_agent(verdict_store):
    return StubAgent(
        model="test-model",
        max_tokens=100,
        verdict_store=verdict_store,
        config={},
    )


@pytest.fixture
def triggered_context():
    return IncidentContext(
        id="INC-2026-0001",
        state=IncidentState.TRIGGERED,
        created_at="2026-03-19T10:00:00Z",
        updated_at="2026-03-19T10:00:00Z",
        trigger_source="nthlayer-correlate",
        trigger_verdict_ids=["vrd-trigger-001"],
        topology={},
    )


async def test_emit_verdict_creates_verdict(stub_agent, verdict_store, triggered_context):
    v = await stub_agent._emit_verdict(
        triggered_context,
        subject_summary="Test triage",
        action="flag",
        confidence=0.8,
        reasoning="test reasoning",
    )
    assert v.subject.type == "triage"
    assert v.producer.system == "nthlayer-respond"
    assert v.judgment.action == "flag"
    assert v.judgment.confidence == 0.8
    assert v.lineage.context == ["vrd-trigger-001"]
    assert v.lineage.parent is None  # first in chain
    # P3-E.1: parent_ids points to upstream triggers for the first verdict
    assert v.parent_ids == ["vrd-trigger-001"]
    assert v.id in triggered_context.verdict_chain
    # Verify persisted
    assert verdict_store.get(v.id) is not None


async def test_emit_verdict_threads_metadata(stub_agent, verdict_store, triggered_context):
    """metadata kwarg is forwarded to verdict_create unchanged.

    Bead 1 (structured remediation emission) needs subclasses to attach
    role-specific structured fields. This test pins the kwarg path.
    """
    v = await stub_agent._emit_verdict(
        triggered_context,
        subject_summary="metadata-bearing verdict",
        action="flag",
        confidence=0.8,
        reasoning="r",
        metadata={"custom": {"some_key": "some_value", "nullable": None}},
    )
    assert v.metadata.custom == {"some_key": "some_value", "nullable": None}


async def test_emit_verdict_metadata_default_is_empty_custom(
    stub_agent, verdict_store, triggered_context,
):
    """When metadata kwarg is omitted, the verdict's metadata.custom is the
    empty dict (Metadata default). Documents the backward-compatible default
    so existing callers keep working."""
    v = await stub_agent._emit_verdict(
        triggered_context,
        subject_summary="no metadata",
        action="flag",
        confidence=0.8,
        reasoning="r",
    )
    assert v.metadata.custom == {}


async def test_emit_verdict_chains_parent(stub_agent, verdict_store, triggered_context):
    v1 = await stub_agent._emit_verdict(
        triggered_context, "first", "flag", 0.8, "first verdict",
    )
    v2 = await stub_agent._emit_verdict(
        triggered_context, "second", "flag", 0.7, "second verdict",
    )
    assert v2.lineage.parent == v1.id
    # P3-E.1: parent_ids on chained verdicts points to immediate predecessor
    assert v2.parent_ids == [v1.id]
    assert len(triggered_context.verdict_chain) == 2


async def test_degraded_verdict(stub_agent, verdict_store, triggered_context):
    v = await stub_agent._degraded_verdict(triggered_context, "model timeout")
    assert v.judgment.action == "escalate"
    assert v.judgment.confidence == 0.0
    assert "degraded" in v.judgment.tags
    assert "human-takeover-required" in v.judgment.tags
    assert v.id in triggered_context.verdict_chain


def test_parse_json_clean(stub_agent):
    result = stub_agent._parse_json('{"key": "value"}')
    assert result == {"key": "value"}


def test_parse_json_markdown_fences(stub_agent):
    result = stub_agent._parse_json('```json\n{"key": "value"}\n```')
    assert result == {"key": "value"}


def test_parse_json_preamble(stub_agent):
    result = stub_agent._parse_json('Here is the JSON:\n{"key": "value"}')
    assert result == {"key": "value"}


def test_parse_json_invalid(stub_agent):
    with pytest.raises(ValueError):
        stub_agent._parse_json("not json at all")


async def test_execute_success(stub_agent, triggered_context, verdict_store):
    model_response = json.dumps({
        "severity": 1,
        "blast_radius": ["payment-api"],
        "reasoning": "Critical service affected",
    })
    with patch.object(stub_agent, "_call_model", new_callable=AsyncMock, return_value=model_response):
        result = await stub_agent.execute(triggered_context)
    assert result.triage is not None
    assert result.triage.severity == 1
    assert len(result.verdict_chain) == 1


async def test_execute_model_failure_degrades(stub_agent, triggered_context, verdict_store):
    with patch.object(stub_agent, "_call_model", new_callable=AsyncMock, side_effect=Exception("API down")):
        result = await stub_agent.execute(triggered_context)
    assert result.triage is None  # no result applied
    assert len(result.verdict_chain) == 1  # degraded verdict emitted
    v = verdict_store.get(result.verdict_chain[0])
    assert v.judgment.action == "escalate"
    assert v.judgment.confidence == 0.0


# ------------------------------------------------------------------ #
# P3-E.1: worker-mode dispatch                                         #
# ------------------------------------------------------------------ #


def test_init_requires_client_or_verdict_store():
    """AgentBase init raises if neither client nor verdict_store is set."""
    with pytest.raises(ValueError, match="must configure exactly one of"):
        StubAgent(model="m", max_tokens=100)


async def test_emit_verdict_worker_mode_uses_client(triggered_context):
    """Worker mode: _emit_verdict submits via CoreAPIClient, not verdict_store."""
    from nthlayer_common.api_client import APIResult

    fake_client = AsyncMock()
    fake_client.submit_verdict = AsyncMock(
        return_value=APIResult(ok=True, status_code=200, data={}, error=None, detail=None)
    )

    agent = StubAgent(
        model="test-model",
        max_tokens=100,
        client=fake_client,
        deployment_id="test-dep",
        config={},
    )
    v = await agent._emit_verdict(
        triggered_context, "test summary", "flag", 0.7, "test reasoning",
    )
    assert fake_client.submit_verdict.await_count == 1
    submitted = fake_client.submit_verdict.await_args[0][0]
    # Submitted payload is the verdict dict (the data inside the CloudEvents envelope)
    assert submitted["id"] == v.id
    assert v.id in triggered_context.verdict_chain


async def test_emit_verdict_worker_mode_submit_failure_does_not_raise(
    triggered_context,
):
    """Worker mode: submission failure is logged + counted but never raised.

    R5 correctness fix: a failed submission must NOT append to verdict_chain.
    If it did, the next agent's parent_ids would point to a verdict that
    doesn't exist in core (dangling lineage reference).
    """
    from nthlayer_common.api_client import APIResult

    fake_client = AsyncMock()
    fake_client.submit_verdict = AsyncMock(
        return_value=APIResult(
            ok=False, status_code=503, data=None, error="server_error", detail=None
        )
    )

    agent = StubAgent(
        model="test-model",
        max_tokens=100,
        client=fake_client,
        config={},
    )
    # Should not raise
    v = await agent._emit_verdict(triggered_context, "s", "flag", 0.5, "r")
    # R5 fix: failed verdict NOT appended → chain stays consistent with core
    assert v.id not in triggered_context.verdict_chain
    assert triggered_context.verdict_chain == []


async def test_emit_verdict_chains_to_last_successful_after_failure(
    triggered_context,
):
    """After a submission failure, the next verdict chains to the trigger
    (or the last successfully submitted verdict), NOT to the failed verdict.
    """
    from nthlayer_common.api_client import APIResult

    fake_client = AsyncMock()
    # First two succeed, third fails, fourth must chain to second (not third)
    responses = [
        APIResult(ok=True, status_code=200, data={}, error=None, detail=None),
        APIResult(ok=True, status_code=200, data={}, error=None, detail=None),
        APIResult(ok=False, status_code=503, data=None, error="x", detail=None),
        APIResult(ok=True, status_code=200, data={}, error=None, detail=None),
    ]
    fake_client.submit_verdict = AsyncMock(side_effect=responses)

    agent = StubAgent(model="m", max_tokens=10, client=fake_client, config={})
    v1 = await agent._emit_verdict(triggered_context, "1", "flag", 0.5, "r")
    v2 = await agent._emit_verdict(triggered_context, "2", "flag", 0.5, "r")
    # v3 deliberately not bound — its submit_verdict mock returns ok=False
    # so it never appends to verdict_chain. The next verdict (v4) chains
    # to v2 instead, which is the assertion below.
    _ = await agent._emit_verdict(triggered_context, "3", "flag", 0.5, "r")
    v4 = await agent._emit_verdict(triggered_context, "4", "flag", 0.5, "r")

    # v1, v2, v4 in chain; v3 (failed) NOT in chain
    assert triggered_context.verdict_chain == [v1.id, v2.id, v4.id]
    # v4's parent_ids points to v2 (last successful predecessor), not v3 (failed)
    assert v4.parent_ids == [v2.id]


async def test_emit_verdict_sets_escalation_pending(triggered_context):
    """P3-E.1: capture-at-write-time. _emit_verdict sets the escalation flag
    when verdict has action=escalate and confidence < threshold."""
    from nthlayer_common.api_client import APIResult

    fake_client = AsyncMock()
    fake_client.submit_verdict = AsyncMock(
        return_value=APIResult(ok=True, status_code=200, data={}, error=None, detail=None)
    )

    agent = StubAgent(
        model="test-model",
        max_tokens=100,
        client=fake_client,
        config={"escalation_threshold": 0.3},
    )
    await agent._emit_verdict(
        triggered_context, "s", "escalate", confidence=0.1, reasoning="r",
    )
    assert triggered_context.metadata.get("escalation_pending") is True


async def test_emit_verdict_no_flag_above_threshold(triggered_context):
    """High-confidence escalate verdict does NOT set escalation_pending."""
    from nthlayer_common.api_client import APIResult

    fake_client = AsyncMock()
    fake_client.submit_verdict = AsyncMock(
        return_value=APIResult(ok=True, status_code=200, data={}, error=None, detail=None)
    )

    agent = StubAgent(
        model="test-model",
        max_tokens=100,
        client=fake_client,
        config={"escalation_threshold": 0.3},
    )
    await agent._emit_verdict(
        triggered_context, "s", "escalate", confidence=0.9, reasoning="r",
    )
    assert "escalation_pending" not in triggered_context.metadata


# -- Structured call path (P3-E.2, opensrm-st4s.2) --

class StructuredStubAgent(AgentBase):
    """Stub that opts into the structured path via response_model."""
    role = AgentRole.TRIAGE
    default_timeout = 5

    # Imported lazily inside the test so the class definition stays light.
    from nthlayer_workers.respond.agents.response_models import TriageResponse
    response_model = TriageResponse

    def build_prompt(self, context):
        return ("You are a test agent.", "Assess.")

    def parse_response(self, response, context):
        data = self._parse_json(response)
        return TriageResult(
            severity=data.get("severity", 2),
            blast_radius=data.get("blast_radius", []),
            affected_slos=data.get("affected_slos", []),
            assigned_team=data.get("assigned_team"),
            reasoning=data.get("reasoning", ""),
        )

    def _apply_result(self, context, result):
        context.triage = result
        return context


async def test_structured_path_routes_through_structured_call(verdict_store, triggered_context):
    """When response_model is set, execute() goes through Instructor."""
    from nthlayer_common.llm_structured import StructuredCallResult, StructuredCallUsage

    from nthlayer_workers.respond.agents.response_models import TriageResponse

    canned = TriageResponse(
        severity=3, blast_radius=["svc-a"], affected_slos=["latency"],
        assigned_team="ops", reasoning="canned", confidence=0.7,
    )

    agent = StructuredStubAgent(
        model="anthropic/claude", max_tokens=100,
        verdict_store=verdict_store, config={},
    )

    with patch(
        "nthlayer_workers.respond.agents.base.structured_call_with_usage",
        return_value=StructuredCallResult(
            data=canned, usage=StructuredCallUsage(input_tokens=12, output_tokens=34),
        ),
    ) as mock_call:
        result = await agent._call_model_structured(
            "system", "user", TriageResponse,
        )

    mock_call.assert_called_once()
    assert mock_call.call_args.kwargs["response_model"] is TriageResponse
    # Returned value is the JSON-stringified validated model.
    parsed = json.loads(result)
    assert parsed["severity"] == 3
    assert parsed["assigned_team"] == "ops"


async def test_structured_path_emits_otel_cost_event(verdict_store):
    """Each structured call emits an OTel event with token usage."""
    from nthlayer_common.llm_structured import StructuredCallResult, StructuredCallUsage

    from nthlayer_workers.respond.agents.response_models import TriageResponse

    canned = TriageResponse(
        severity=2, blast_radius=[], affected_slos=[],
        assigned_team=None, reasoning="", confidence=0.5,
    )

    agent = StructuredStubAgent(
        model="anthropic/claude", max_tokens=100,
        verdict_store=verdict_store, config={},
    )

    with patch(
        "nthlayer_workers.respond.agents.base.structured_call_with_usage",
        return_value=StructuredCallResult(
            data=canned, usage=StructuredCallUsage(input_tokens=42, output_tokens=99),
        ),
    ), patch(
        "nthlayer_workers.respond.agents.base.emit_llm_event",
    ) as mock_emit:
        await agent._call_model_structured("s", "u", TriageResponse)

    mock_emit.assert_called_once()
    kwargs = mock_emit.call_args.kwargs
    assert kwargs["model"] == "anthropic/claude"
    assert kwargs["provider"] == "anthropic"
    assert kwargs["caller"] == "respond.triage"
    assert kwargs["input_tokens"] == 42
    assert kwargs["output_tokens"] == 99
    assert kwargs["success"] is True


async def test_response_model_none_uses_raw_text_path(verdict_store):
    """response_model=None falls back to _call_model (raw text)."""
    agent = StubAgent(
        model="test-model", max_tokens=100,
        verdict_store=verdict_store, config={},
    )
    assert agent.response_model is None
    # The fixtures don't exercise the network; just verify the attribute
    # contract that controls routing.


async def test_structured_path_emits_otel_event_on_failure(verdict_store):
    """Failed structured calls also emit an OTel event (cost-accounting parity)."""
    from nthlayer_common.llm import LLMError

    from nthlayer_workers.respond.agents.response_models import TriageResponse

    agent = StructuredStubAgent(
        model="anthropic/claude", max_tokens=100,
        verdict_store=verdict_store, config={},
    )

    with patch(
        "nthlayer_workers.respond.agents.base.structured_call_with_usage",
        side_effect=LLMError("boom", provider="anthropic", model="claude"),
    ), patch(
        "nthlayer_workers.respond.agents.base.emit_llm_event",
    ) as mock_emit, pytest.raises(LLMError):
        await agent._call_model_structured("s", "u", TriageResponse)

    mock_emit.assert_called_once()
    kwargs = mock_emit.call_args.kwargs
    assert kwargs["success"] is False
    assert kwargs["error"] == "LLMError"
    assert kwargs["caller"] == "respond.triage"
    # Token fields are absent on failure (no usage data available).
    assert "input_tokens" not in kwargs or kwargs["input_tokens"] is None
