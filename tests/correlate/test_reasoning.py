# tests/test_reasoning.py
"""Tests for the reasoning layer — model-based and degraded paths."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from nthlayer_workers.correlate.reasoning import (
    _build_system_prompt,
    _build_user_prompt,
    _degraded_reasoning,
    _parse_reasoning_response,
    reason_about_correlations,
    reasoning_available,
)
from nthlayer_workers.correlate.types import (
    ChangeCandidate,
    CorrelationGroup,
    EventType,
    SitRepEvent,
    TemporalGroup,
    TopologyCorrelation,
)

# --- Fixtures ---

def _make_group(
    group_id: str = "cg-test0001",
    services: list[str] | None = None,
    priority: int = 1,
    change_service: str | None = None,
) -> CorrelationGroup:
    """Build a minimal CorrelationGroup for testing."""
    services = services or ["fraud-detect"]
    alert_event = SitRepEvent(
        id="evt-alert-1",
        timestamp="2026-03-25T10:00:00Z",
        source="prometheus",
        type=EventType.ALERT,
        service=services[0],
        environment="production",
        severity=0.85,
        payload={"alert_name": "HighReversalRate", "value": 0.08},
    )
    signal = TemporalGroup(
        service=services[0],
        time_window=("2026-03-25T09:58:00Z", "2026-03-25T10:01:00Z"),
        events=[alert_event],
        count=1,
        peak_severity=0.85,
        duration_seconds=180.0,
    )
    change_candidates = []
    if change_service:
        change_evt = SitRepEvent(
            id="evt-change-1",
            timestamp="2026-03-25T09:50:00Z",
            source="github",
            type=EventType.CHANGE,
            service=change_service,
            environment="production",
            severity=0.1,
            payload={"change_type": "deploy", "detail": "model v2.3 rollout"},
        )
        change_candidates.append(ChangeCandidate(
            change=change_evt,
            affected_service=services[0],
            temporal_proximity_seconds=600.0,
            same_service=change_service == services[0],
            dependency_related=change_service != services[0],
        ))

    topology = None
    if len(services) > 1:
        topology = TopologyCorrelation(
            primary_service=services[0],
            related_services=[{"service": s, "relationship": "depends_on", "events": []} for s in services[1:]],
            topology_path=services,
        )

    return CorrelationGroup(
        id=group_id,
        priority=priority,
        summary=f"{signal.count} alert(s) on {', '.join(services)}",
        services=services,
        signals=[signal],
        topology=topology,
        change_candidates=change_candidates,
        first_seen=signal.time_window[0],
        last_updated=signal.time_window[1],
        event_count=signal.count,
    )


SAMPLE_DEP_GRAPH = {
    "fraud-detect": {"tier": "critical", "dependencies": ["feature-store"], "dependents": ["payment-api"]},
    "payment-api": {"tier": "critical", "dependencies": ["fraud-detect"], "dependents": ["checkout-svc"]},
    "feature-store": {"tier": "standard", "dependencies": [], "dependents": ["fraud-detect"]},
}

MOCK_MODEL_RESPONSE = json.dumps({
    "groups": [
        {
            "group_id": "cg-test0001",
            "root_cause": "model v2.3 deployment caused reversal rate spike",
            "confidence": 0.87,
            "reasoning": "Deploy occurred 10 minutes before breach. fraud-detect is an AI gate; reversal rate is a judgment SLO. The deploy touched the model version, which directly affects decision quality.",
            "recommended_actions": ["Roll back model to v2.2", "Check reversal rate SLO after rollback"],
            "is_causal": True,
        }
    ],
    "overall_assessment": "High-confidence causal link between model v2.3 deploy and reversal rate breach on fraud-detect. Recommend immediate model rollback.",
    "overall_confidence": 0.87,
})


# --- _degraded_reasoning tests ---

class TestDegradedReasoning:
    def test_returns_degraded_structure(self):
        group = _make_group()
        result = _degraded_reasoning([group])
        assert result["degraded"] is True
        assert result["overall_confidence"] == 0.0
        assert len(result["groups"]) == 1
        assert result["groups"][0]["confidence"] == 0.0
        assert result["groups"][0]["degraded"] is True
        assert result["groups"][0]["root_cause"] is None

    def test_empty_groups(self):
        result = _degraded_reasoning([])
        assert result["degraded"] is True
        assert result["groups"] == []
        assert result["overall_confidence"] == 0.0

    def test_includes_reason(self):
        group = _make_group()
        result = _degraded_reasoning([group], reason="API timeout")
        assert "API timeout" in result["groups"][0]["reasoning"]
        assert "API timeout" in result["overall_assessment"]


# --- _parse_reasoning_response tests ---

class TestParseReasoningResponse:
    def test_valid_response(self):
        group = _make_group()
        result = _parse_reasoning_response(MOCK_MODEL_RESPONSE, [group])
        assert result["degraded"] is False
        assert result["overall_confidence"] == 0.87
        assert len(result["groups"]) == 1
        ga = result["groups"][0]
        assert ga["group_id"] == "cg-test0001"
        assert ga["confidence"] == 0.87
        assert ga["root_cause"] is not None
        assert len(ga["recommended_actions"]) == 2

    def test_markdown_fenced_response(self):
        group = _make_group()
        fenced = f"```json\n{MOCK_MODEL_RESPONSE}\n```"
        result = _parse_reasoning_response(fenced, [group])
        assert result["overall_confidence"] == 0.87

    def test_unknown_group_id_filtered(self):
        group = _make_group(group_id="cg-different")
        result = _parse_reasoning_response(MOCK_MODEL_RESPONSE, [group])
        # cg-test0001 from response doesn't match cg-different
        assert len(result["groups"]) == 0

    def test_confidence_clamped(self):
        group = _make_group()
        response = json.dumps({
            "groups": [{"group_id": "cg-test0001", "confidence": 1.5, "reasoning": "test"}],
            "overall_confidence": -0.3,
        })
        result = _parse_reasoning_response(response, [group])
        assert result["groups"][0]["confidence"] == 1.0
        assert result["overall_confidence"] == 0.0

    def test_malformed_json_raises(self):
        group = _make_group()
        with pytest.raises(json.JSONDecodeError):
            _parse_reasoning_response("not json at all", [group])


# --- _build_user_prompt tests ---

class TestBuildUserPrompt:
    def test_includes_dependency_graph(self):
        group = _make_group(change_service="fraud-detect")
        prompt = _build_user_prompt([group], SAMPLE_DEP_GRAPH, None)
        assert "DEPENDENCY GRAPH" in prompt
        assert "fraud-detect" in prompt
        assert "payment-api" in prompt

    def test_includes_group_details(self):
        group = _make_group(group_id="cg-abc12345", change_service="fraud-detect")
        prompt = _build_user_prompt([group], {}, None)
        assert "cg-abc12345" in prompt
        assert "fraud-detect" in prompt
        assert "Change candidate" in prompt

    def test_includes_slo_targets(self):
        prompt = _build_user_prompt(
            [_make_group()], {},
            {"fraud-detect": {"reversal_rate": {"target": 0.015, "window": "2m"}}},
        )
        assert "SLO TARGETS" in prompt
        assert "reversal_rate" in prompt

    def test_topology_included(self):
        group = _make_group(services=["fraud-detect", "payment-api"])
        prompt = _build_user_prompt([group], SAMPLE_DEP_GRAPH, None)
        assert "Topology:" in prompt


class TestBuildSystemPrompt:
    def test_contains_key_instructions(self):
        prompt = _build_system_prompt()
        assert "dependency" in prompt.lower()
        assert "temporal" in prompt.lower()
        assert "cascading" in prompt.lower()
        assert "JSON" in prompt


class TestReasoningAvailable:
    def test_available_with_anthropic_key(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("NTHLAYER_MODEL", raising=False)
        assert reasoning_available() is True

    def test_available_with_openai_key(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "test")
        monkeypatch.delenv("NTHLAYER_MODEL", raising=False)
        assert reasoning_available() is True

    def test_available_with_nthlayer_model(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("NTHLAYER_MODEL", "ollama/llama3.1")
        assert reasoning_available() is True

    def test_not_available_without_keys(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("NTHLAYER_MODEL", raising=False)
        assert reasoning_available() is False


# --- reason_about_correlations integration tests ---

class TestReasonAboutCorrelations:
    @pytest.mark.asyncio
    async def test_successful_reasoning(self):
        group = _make_group(change_service="fraud-detect")

        async def mock_call_model(system, user, model, max_tokens, timeout):
            return MOCK_MODEL_RESPONSE

        with patch("nthlayer_workers.correlate.reasoning._call_model", side_effect=mock_call_model):
            result = await reason_about_correlations(
                [group], SAMPLE_DEP_GRAPH, timeout=5,
            )

        assert result["degraded"] is False
        assert result["overall_confidence"] == 0.87
        assert len(result["groups"]) == 1
        assert result["groups"][0]["root_cause"] is not None

    @pytest.mark.asyncio
    async def test_empty_groups_returns_degraded(self):
        result = await reason_about_correlations([], {})
        assert result["degraded"] is True
        assert result["overall_confidence"] == 0.0

    @pytest.mark.asyncio
    async def test_api_error_returns_degraded(self):
        group = _make_group()

        async def mock_call_model(system, user, model, max_tokens, timeout):
            raise Exception("API key invalid")

        with patch("nthlayer_workers.correlate.reasoning._call_model", side_effect=mock_call_model):
            result = await reason_about_correlations(
                [group], SAMPLE_DEP_GRAPH, timeout=5,
            )

        assert result["degraded"] is True
        assert result["overall_confidence"] == 0.0

    @pytest.mark.asyncio
    async def test_malformed_response_returns_degraded(self):
        group = _make_group()

        async def mock_call_model(system, user, model, max_tokens, timeout):
            return "this is not json"

        with patch("nthlayer_workers.correlate.reasoning._call_model", side_effect=mock_call_model):
            result = await reason_about_correlations(
                [group], SAMPLE_DEP_GRAPH, timeout=5,
            )

        assert result["degraded"] is True
        assert result["overall_confidence"] == 0.0

    @pytest.mark.asyncio
    async def test_timeout_returns_degraded(self):

        group = _make_group()

        async def mock_call_model(system, user, model, max_tokens, timeout):
            raise TimeoutError()

        with patch("nthlayer_workers.correlate.reasoning._call_model", side_effect=mock_call_model):
            result = await reason_about_correlations(
                [group], SAMPLE_DEP_GRAPH, timeout=1,
            )

        assert result["degraded"] is True


# --- CLI integration: --no-reasoning produces heuristic output ---

class TestCorrelateCommandReasoning:
    def test_no_reasoning_produces_heuristic_verdict(self, tmp_path):
        """--no-reasoning flag produces verdict with reasoning_mode=heuristic."""
        from nthlayer_common.verdicts import SQLiteVerdictStore
        from nthlayer_common.verdicts import create as verdict_create

        from nthlayer_workers.correlate.cli import correlate_command

        store_path = str(tmp_path / "verdicts.db")
        store = SQLiteVerdictStore(store_path)
        trigger = verdict_create(
            subject={"type": "evaluation", "ref": "fraud-detect", "summary": "breach"},
            judgment={"action": "flag", "confidence": 0.9},
            producer={"system": "nthlayer-measure"},
            metadata={"custom": {"slo_name": "reversal_rate", "breach": True}},
        )
        store.put(trigger)

        specs_dir = tmp_path / "specs"
        specs_dir.mkdir()
        (specs_dir / "fraud-detect.yaml").write_text(
            "apiVersion: srm/v1\nkind: ServiceReliabilityManifest\n"
            "metadata:\n  name: fraud-detect\n  tier: critical\n"
            "spec:\n  type: ai-gate\n  dependencies: []\n"
        )

        async def mock_alerts(client, url, services):
            from nthlayer_workers.correlate.types import EventType, SitRepEvent
            return [SitRepEvent(
                id="alert-1", timestamp="2026-03-25T10:00:00Z",
                source="prometheus", type=EventType.ALERT,
                service="fraud-detect", environment="production",
                severity=0.85, payload={"alert_name": "HighReversalRate"},
            )]

        async def mock_breaches(client, url, services, window_minutes=30):
            return []

        with patch("nthlayer_workers.correlate.prometheus.fetch_alerts", side_effect=mock_alerts), \
             patch("nthlayer_workers.correlate.prometheus.fetch_metric_breaches", side_effect=mock_breaches):
            result = correlate_command(
                trigger_verdict_id=trigger.id,
                prometheus_url="http://mock:9090",
                specs_dir=str(specs_dir),
                verdict_store_path=store_path,
                reasoning=False,
            )

        assert result == 0

        from nthlayer_workers.observe.sqlite_store import SQLiteAssessmentStore
        from nthlayer_workers.observe.store import AssessmentFilter
        asm_store = SQLiteAssessmentStore(store_path)
        rows = asm_store.query(AssessmentFilter(kind="correlation_snapshot", limit=10))
        assert rows
        custom = rows[0].data
        assert custom["reasoning_mode"] == "heuristic"
        assert custom["reasoning"] is None

    def test_reasoning_enabled_without_api_key_falls_back(self, tmp_path, monkeypatch):
        """reasoning=True but no API key falls back to heuristic."""
        from nthlayer_common.verdicts import SQLiteVerdictStore
        from nthlayer_common.verdicts import create as verdict_create

        from nthlayer_workers.correlate.cli import correlate_command

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        store_path = str(tmp_path / "verdicts.db")
        store = SQLiteVerdictStore(store_path)
        trigger = verdict_create(
            subject={"type": "evaluation", "ref": "fraud-detect", "summary": "breach"},
            judgment={"action": "flag", "confidence": 0.9},
            producer={"system": "nthlayer-measure"},
            metadata={"custom": {"slo_name": "reversal_rate", "breach": True}},
        )
        store.put(trigger)

        specs_dir = tmp_path / "specs"
        specs_dir.mkdir()
        (specs_dir / "fraud-detect.yaml").write_text(
            "apiVersion: srm/v1\nkind: ServiceReliabilityManifest\n"
            "metadata:\n  name: fraud-detect\n  tier: critical\n"
            "spec:\n  type: ai-gate\n  dependencies: []\n"
        )

        async def mock_alerts(client, url, services):
            from nthlayer_workers.correlate.types import EventType, SitRepEvent
            return [SitRepEvent(
                id="alert-1", timestamp="2026-03-25T10:00:00Z",
                source="prometheus", type=EventType.ALERT,
                service="fraud-detect", environment="production",
                severity=0.85, payload={"alert_name": "HighReversalRate"},
            )]

        async def mock_breaches(client, url, services, window_minutes=30):
            return []

        with patch("nthlayer_workers.correlate.prometheus.fetch_alerts", side_effect=mock_alerts), \
             patch("nthlayer_workers.correlate.prometheus.fetch_metric_breaches", side_effect=mock_breaches):
            result = correlate_command(
                trigger_verdict_id=trigger.id,
                prometheus_url="http://mock:9090",
                specs_dir=str(specs_dir),
                verdict_store_path=store_path,
                reasoning=True,
            )

        assert result == 0
        from nthlayer_workers.observe.sqlite_store import SQLiteAssessmentStore
        from nthlayer_workers.observe.store import AssessmentFilter
        asm_store = SQLiteAssessmentStore(store_path)
        rows = asm_store.query(AssessmentFilter(kind="correlation_snapshot", limit=10))
        assert rows
        assert rows[0].data["reasoning_mode"] == "heuristic"

    def test_reasoning_enabled_with_model_success(self, tmp_path, monkeypatch):
        """reasoning=True with API key and successful model call sets reasoning_mode=model."""
        from nthlayer_common.verdicts import SQLiteVerdictStore
        from nthlayer_common.verdicts import create as verdict_create

        from nthlayer_workers.correlate.cli import correlate_command

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

        store_path = str(tmp_path / "verdicts.db")
        store = SQLiteVerdictStore(store_path)
        trigger = verdict_create(
            subject={"type": "evaluation", "ref": "fraud-detect", "summary": "breach"},
            judgment={"action": "flag", "confidence": 0.9},
            producer={"system": "nthlayer-measure"},
            metadata={"custom": {"slo_name": "reversal_rate", "breach": True}},
        )
        store.put(trigger)

        specs_dir = tmp_path / "specs"
        specs_dir.mkdir()
        (specs_dir / "fraud-detect.yaml").write_text(
            "apiVersion: srm/v1\nkind: ServiceReliabilityManifest\n"
            "metadata:\n  name: fraud-detect\n  tier: critical\n"
            "spec:\n  type: ai-gate\n  dependencies: []\n"
        )

        async def mock_alerts(client, url, services):
            from nthlayer_workers.correlate.types import EventType, SitRepEvent
            return [SitRepEvent(
                id="alert-1", timestamp="2026-03-25T10:00:00Z",
                source="prometheus", type=EventType.ALERT,
                service="fraud-detect", environment="production",
                severity=0.85, payload={"alert_name": "HighReversalRate"},
            )]

        async def mock_breaches(client, url, services, window_minutes=30):
            return []

        # The model response needs to reference the actual group ID that
        # the engine will generate, which we don't know in advance.
        # So we mock reason_about_correlations to return a known result.
        async def mock_reasoning(groups, dep_graph, **kwargs):
            return {
                "groups": [{
                    "group_id": groups[0].id,
                    "root_cause": "model v2.3 deploy caused reversal rate spike",
                    "confidence": 0.87,
                    "reasoning": "Deploy preceded breach by 10 minutes",
                    "recommended_actions": ["Roll back to v2.2"],
                    "is_causal": True,
                    "degraded": False,
                }],
                "overall_assessment": "Causal link: model deploy → reversal rate breach",
                "overall_confidence": 0.87,
                "degraded": False,
            }

        with patch("nthlayer_workers.correlate.prometheus.fetch_alerts", side_effect=mock_alerts), \
             patch("nthlayer_workers.correlate.prometheus.fetch_metric_breaches", side_effect=mock_breaches), \
             patch("nthlayer_workers.correlate.reasoning.reason_about_correlations", side_effect=mock_reasoning):
            result = correlate_command(
                trigger_verdict_id=trigger.id,
                prometheus_url="http://mock:9090",
                specs_dir=str(specs_dir),
                verdict_store_path=store_path,
                reasoning=True,
            )

        assert result == 0
        from nthlayer_workers.observe.sqlite_store import SQLiteAssessmentStore
        from nthlayer_workers.observe.store import AssessmentFilter
        asm_store = SQLiteAssessmentStore(store_path)
        rows = asm_store.query(AssessmentFilter(kind="correlation_snapshot", limit=10))
        assert rows
        custom = rows[0].data
        assert custom["reasoning_mode"] == "model"
        assert custom["reasoning"] is not None
        # opensrm-saun.1.2.1: assertions migrated to assessment shape.
        # Confidence and summary live in assessment.data (the verdict's
        # judgment.confidence / subject.summary equivalents).
        assert custom["confidence"] == 0.87
        assert "Causal link" in custom["summary"]


# ---------------------------------------------------------------------------
# Task 6b: Trace evidence section in reasoning prompt
# ---------------------------------------------------------------------------

from datetime import UTC
from datetime import datetime as dt

from nthlayer_workers.correlate.reasoning import _build_trace_evidence_section
from nthlayer_workers.correlate.traces.protocol import (
    ErrorSummary,
    OperationLatency,
    ServiceCallEdge,
    ServiceTraceProfile,
    TopologyDivergence,
    TraceEvidence,
)


def _make_trace_evidence_for_reasoning() -> TraceEvidence:
    now = dt.now(tz=UTC)
    return TraceEvidence(
        services=[ServiceTraceProfile(
            service="fraud-detect",
            time_window_start=now, time_window_end=now,
            callers=[ServiceCallEdge(
                source_service="payment-api", target_service="fraud-detect",
                request_count=1204, error_count=0, p50_latency_ms=12.5, p99_latency_ms=85.0,
            )],
            callees=[ServiceCallEdge(
                source_service="fraud-detect", target_service="model-store",
                request_count=1204, error_count=0, p50_latency_ms=10.0, p99_latency_ms=45.0,
            )],
            p50_latency_ms=50.0, p95_latency_ms=120.0, p99_latency_ms=340.0,
            baseline_p50_ms=18.0, latency_change_pct=178.0,
            error_rate=0.02, error_count=24, total_request_count=1204,
            top_errors=[ErrorSummary(
                error_message="timeout waiting for model",
                count=24, first_seen=now, last_seen=now, sample_trace_id="abc",
            )],
            slow_operations=[OperationLatency(
                operation="POST /predict", p50_ms=150.0, p99_ms=312.0,
                request_count=1204, error_rate=0.02,
                baseline_p50_ms=50.0, change_pct=200.0,
            )],
            sample_error_traces=[], sample_slow_traces=[],
        )],
        topology_divergence=None,
        query_time_ms=6240.0,
        backend="tempo",
    )


class TestBuildTraceEvidenceSection:
    def test_with_data(self):
        evidence = _make_trace_evidence_for_reasoning()
        section = _build_trace_evidence_section(evidence)
        assert "fraud-detect" in section
        assert "340" in section  # p99
        assert "vs baseline" in section or "baseline" in section
        assert "2.0%" in section or "error" in section.lower()
        assert "POST /predict" in section
        assert "payment-api" in section  # caller
        assert "model-store" in section  # callee

    def test_none_returns_empty(self):
        assert _build_trace_evidence_section(None) == ""

    def test_empty_services_returns_empty(self):
        evidence = TraceEvidence(
            services=[], topology_divergence=None,
            query_time_ms=0, backend="tempo",
        )
        assert _build_trace_evidence_section(evidence) == ""

    def test_topology_divergence_included(self):
        evidence = _make_trace_evidence_for_reasoning()
        evidence.topology_divergence = TopologyDivergence(
            declared_not_observed=[],
            observed_not_declared=[("fraud-detect", "unknown-svc")],
        )
        section = _build_trace_evidence_section(evidence)
        assert "Undeclared" in section or "undeclared" in section.lower()
        assert "unknown-svc" in section

    def test_within_baseline_latency(self):
        now = dt.now(tz=UTC)
        evidence = TraceEvidence(
            services=[ServiceTraceProfile(
                service="stable-svc",
                time_window_start=now, time_window_end=now,
                callers=[], callees=[],
                p50_latency_ms=10.0, p95_latency_ms=20.0, p99_latency_ms=30.0,
                baseline_p50_ms=9.5, latency_change_pct=5.0,  # <10%
                error_rate=0.0, error_count=0, total_request_count=500,
                top_errors=[], slow_operations=[],
                sample_error_traces=[], sample_slow_traces=[],
            )],
            topology_divergence=None,
            query_time_ms=100.0,
            backend="tempo",
        )
        section = _build_trace_evidence_section(evidence)
        assert "within baseline" in section


class TestBuildUserPromptWithTraceEvidence:
    def test_includes_trace_section(self):
        evidence = _make_trace_evidence_for_reasoning()
        group = _make_group()
        prompt = _build_user_prompt([group], {}, None, trace_evidence=evidence)
        assert "Trace Evidence" in prompt or "TRACE EVIDENCE" in prompt

    def test_without_trace_unchanged(self):
        group = _make_group()
        prompt_without = _build_user_prompt([group], {}, None)
        prompt_with_none = _build_user_prompt([group], {}, None, trace_evidence=None)
        assert prompt_without == prompt_with_none
        assert "Trace Evidence" not in prompt_without
