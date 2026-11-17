"""Respond verdicts carry the incident's service [opensrm-dm33].

Core stores a verdict's top-level ``service`` in its ``service`` column
(nthlayer-core ``store.py`` ``put_verdict``: ``verdict.get("service")``) and
filters ``GET /verdicts?service=`` on it. Respond's verdicts had none, so a
query by service never returned triage, investigation, communication or
remediation verdicts.

Fixture provenance: the quality_breach here is what the real ``MeasureModule``
submits; the correlation_snapshot has the shape correlate emits
(``correlate/worker.py``: ``data.domain.service``).
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

from nthlayer_common.api_client import APIResult
from nthlayer_common.verdicts import MemoryStore

from nthlayer_workers.measure.worker import MeasureModule
from nthlayer_workers.respond.agents.base import AgentBase
from nthlayer_workers.respond.cli import replay_command
from nthlayer_workers.respond.config import RespondConfig
from nthlayer_workers.respond.context_store import (
    SQLiteContextStore,
    incident_context_from_dict,
    incident_context_to_dict,
)
from nthlayer_workers.respond.types import (
    AgentRole,
    IncidentContext,
    IncidentState,
    TriageResult,
)
from nthlayer_workers.respond.worker_helpers import open_from_breach, open_from_snapshot
from tests.measure.test_measure_worker import _manifest_with_judgment_slos


def _ok(data=None, status_code=200):
    return APIResult(ok=True, status_code=status_code, data=data if data is not None else {})


class _Agent(AgentBase):
    role = AgentRole.TRIAGE
    default_timeout = 5

    def build_prompt(self, context):
        return ("system", "user")

    def parse_response(self, response, context):
        return TriageResult(severity=2, blast_radius=[], affected_slos=[],
                            assigned_team=None, reasoning="")

    def _apply_result(self, context, result):
        context.triage = result
        return context


async def _measure_breach() -> dict:
    """The quality_breach verdict a breaching MeasureModule cycle submits."""
    with patch("nthlayer_workers.measure.worker.PrometheusProvider") as prov_cls:
        prov = AsyncMock()
        prov.get_sli_value = AsyncMock(return_value=0.92)  # below the 98.5 target
        prov.aclose = AsyncMock()
        prov_cls.return_value = prov
        client = AsyncMock()
        client.get_manifests = AsyncMock(return_value=_manifest_with_judgment_slos())
        client.submit_assessment = AsyncMock(return_value=_ok(status_code=201))
        sent: list[dict] = []

        async def _capture(envelope):
            sent.append(envelope["data"])
            return _ok(status_code=201)

        client.submit_verdict = AsyncMock(side_effect=_capture)
        await MeasureModule(client=client, prometheus_url="http://prom:9090").process_cycle()
    breaches = [v for v in sent if v["type"] == "quality_breach"]
    assert len(breaches) == 1
    return breaches[0]


def _snapshot(domain: dict) -> dict:
    return {
        "id": "csn-1",
        "created_at": "2026-04-25T12:00:00+00:00",
        "kind": "correlation_snapshot",
        "service": domain.get("service", "unknown"),
        "data": {
            "domain": domain,
            "parent_ids": ["vrd-breach-1"],
            "affected_services": ["fraud-detect", "payment-api"],
            "blast_radius": [],
            "peak_severity": 0.9,
            "event_count": 5,
            "nl_summary": "summary",
        },
    }


def _worker_agent() -> tuple[_Agent, list[dict]]:
    """An agent in worker mode and the envelope ``data`` dicts it submits,
    which is what core stores."""
    submitted: list[dict] = []

    async def _capture(envelope):
        submitted.append(envelope["data"])
        return _ok(status_code=201)

    client = AsyncMock()
    client.submit_verdict = AsyncMock(side_effect=_capture)
    agent = _Agent(model="m", max_tokens=10, client=client, deployment_id="dep", config={})
    return agent, submitted


async def _emit(agent: _Agent, context: IncidentContext):
    return await agent._emit_verdict(context, "summary", "flag", 0.8, "reasoning")


class TestIncidentService:
    async def test_breach_incident_takes_the_breachs_service(self):
        breach = await _measure_breach()
        ctx = open_from_breach(breach, RespondConfig())
        assert ctx.service == breach["service"] == "fraud-detect"

    def test_snapshot_incident_takes_the_domain_service(self):
        snap = _snapshot({"service": "fraud-detect", "environment": "production"})
        ctx = open_from_snapshot(snap, RespondConfig())
        assert ctx.service == "fraud-detect"

    def test_snapshot_without_a_domain_service_is_unknown(self):
        """The placeholder the incident's id, topology and case already use."""
        ctx = open_from_snapshot(_snapshot({}), RespondConfig())
        assert ctx.service == "unknown"
        assert ctx.topology["services"][0]["name"] == "fraud-detect"


class TestVerdictService:
    async def test_submitted_verdict_carries_the_service_core_stores(self):
        breach = await _measure_breach()
        ctx = open_from_breach(breach, RespondConfig())
        agent, submitted = _worker_agent()

        await _emit(agent, ctx)

        assert len(submitted) == 1
        assert submitted[0]["service"] == "fraud-detect"
        assert submitted[0]["subject"]["service"] == "fraud-detect"

    async def test_every_verdict_in_the_chain_carries_it(self):
        ctx = open_from_snapshot(
            _snapshot({"service": "fraud-detect", "environment": "production"}), RespondConfig()
        )
        agent, submitted = _worker_agent()

        await _emit(agent, ctx)
        await _emit(agent, ctx)

        assert [v["service"] for v in submitted] == ["fraud-detect", "fraud-detect"]
        assert submitted[1]["parent_ids"] == [submitted[0]["id"]]

    async def test_local_store_mode_carries_it(self):
        ctx = open_from_snapshot(
            _snapshot({"service": "fraud-detect", "environment": "production"}), RespondConfig()
        )
        store = MemoryStore()
        agent = _Agent(model="m", max_tokens=10, verdict_store=store, config={})

        v = await _emit(agent, ctx)

        stored = store.get(v.id)
        assert stored.service == "fraud-detect"
        assert stored.subject.service == "fraud-detect"

    async def test_incident_without_a_service_emits_none(self):
        ctx = IncidentContext(
            id="INC-2026-0001", state=IncidentState.TRIGGERED,
            created_at="2026-03-19T10:00:00Z", updated_at="2026-03-19T10:00:00Z",
            trigger_source="nthlayer-correlate", trigger_verdict_ids=["vrd-1"], topology={},
        )
        agent, submitted = _worker_agent()

        await _emit(agent, ctx)

        assert submitted[0]["service"] is None
        assert submitted[0]["subject"]["service"] is None


class TestServiceSurvivesPersistence:
    def test_worker_state_round_trip(self):
        ctx = open_from_snapshot(
            _snapshot({"service": "fraud-detect", "environment": "production"}), RespondConfig()
        )
        restored = incident_context_from_dict(incident_context_to_dict(ctx))
        assert restored.service == "fraud-detect"

    def test_context_saved_before_the_field_restores_as_none(self):
        ctx = open_from_snapshot(
            _snapshot({"service": "fraud-detect", "environment": "production"}), RespondConfig()
        )
        old = incident_context_to_dict(ctx)
        del old["service"]
        assert incident_context_from_dict(old).service is None

    def test_context_store_round_trip(self, tmp_path):
        store = SQLiteContextStore(str(tmp_path / "ctx.db"))
        try:
            ctx = open_from_snapshot(
                _snapshot({"service": "fraud-detect", "environment": "production"}),
                RespondConfig(),
            )
            store.save(ctx)
            assert store.load(ctx.id).service == "fraud-detect"
        finally:
            store.close()


class TestCliPaths:
    async def test_pagerduty_replay_takes_the_alerts_service(self, tmp_path):
        scenario_path = os.path.join(
            os.path.dirname(__file__), "..", "scenarios", "synthetic", "sitrep-unavailable.yaml"
        )
        result = await replay_command(
            scenario_path, config_path=None, no_model=True, work_dir=str(tmp_path)
        )
        assert result["context"].service == "payment-api"
