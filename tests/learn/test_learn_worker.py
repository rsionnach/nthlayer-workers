"""Tests for learn worker modules — outcome resolution and retrospective generation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock


from nthlayer_common.api_client import APIResult
from nthlayer_workers.learn.worker import (
    LearnOutcomeModule,
    LearnRetrospectiveModule,
    _build_chain_timeline,
    _generate_recommendations,
)


# ---------------------------------------------------------------------------
# LearnOutcomeModule
# ---------------------------------------------------------------------------


class TestOutcomeModuleProtocol:
    def test_name(self):
        module = LearnOutcomeModule(client=AsyncMock())
        assert module.name == "learn.outcome"

    async def test_restore_state_accepts_none(self):
        module = LearnOutcomeModule(client=AsyncMock())
        await module.restore_state(None)

    async def test_get_state_empty(self):
        module = LearnOutcomeModule(client=AsyncMock())
        assert await module.get_state() == {}

    def test_satisfies_protocol(self):
        from nthlayer_workers.runner import WorkerModule
        module = LearnOutcomeModule(client=AsyncMock())
        assert isinstance(module, WorkerModule)

    async def test_restore_state_restores_cursor(self):
        module = LearnOutcomeModule(client=AsyncMock())
        await module.restore_state({"cursor": "2026-04-24T10:00:00+00:00"})
        assert module._cursor == "2026-04-24T10:00:00+00:00"


class TestOutcomeCycle:
    async def test_no_pending_verdicts_noop(self):
        client = AsyncMock()
        client.get_verdicts = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=[]))
        client.resolve_outcome = AsyncMock()

        module = LearnOutcomeModule(client=client)
        await module.process_cycle()

        client.resolve_outcome.assert_not_awaited()

    async def test_resolution_via_lineage(self):
        """Downstream execution verdict resolves parent via lineage path."""
        old_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        client = AsyncMock()
        client.get_verdicts = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=[
            {
                "id": "vrd-001",
                "created_at": old_time,
                "type": "approval",
                "service": "fraud-detect",
                "outcome": {"status": "pending"},
                "judgment": {"confidence": 0.9},
                "producer": {"system": "nthlayer-respond"},
            }
        ]))
        client.get_descendants = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=[
            {"id": "vrd-002", "type": "execution", "outcome": {"status": "confirmed"}},
        ]))
        client.resolve_outcome = AsyncMock(return_value=APIResult(ok=True, status_code=201, data={}))
        client.submit_assessment = AsyncMock(return_value=APIResult(ok=True, status_code=201, data={}))

        module = LearnOutcomeModule(client=client)
        await module.process_cycle()

        client.resolve_outcome.assert_awaited_once()
        # Calibration signal should also be emitted
        client.submit_assessment.assert_awaited_once()
        submitted = client.submit_assessment.call_args[0][0]
        submitted = submitted.get('data', submitted)
        assert submitted["kind"] == "calibration_signal"

    async def test_resolution_via_expiry(self):
        """Old pending verdict past threshold marked expired."""
        very_old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        client = AsyncMock()
        client.get_verdicts = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=[
            {
                "id": "vrd-old",
                "created_at": very_old,
                "type": "action_request",
                "service": "svc",
                "outcome": {"status": "pending"},
                "judgment": {},
                "producer": {},
            }
        ]))
        client.get_descendants = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=[]))
        client.resolve_outcome = AsyncMock(return_value=APIResult(ok=True, status_code=201, data={}))
        client.submit_assessment = AsyncMock()

        module = LearnOutcomeModule(client=client, expiry_threshold_days=7)
        await module.process_cycle()

        client.resolve_outcome.assert_awaited_once()
        outcome = client.resolve_outcome.call_args[0][1]
        assert outcome["outcome_status"] == "expired"
        # Expired verdicts don't produce calibration signals
        client.submit_assessment.assert_not_awaited()

    async def test_calibration_signal_has_delta(self):
        """Calibration signal includes correct delta computation."""
        old_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        client = AsyncMock()
        client.get_verdicts = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=[
            {
                "id": "vrd-003",
                "created_at": old_time,
                "type": "approval",
                "service": "svc",
                "outcome": {"status": "pending"},
                "judgment": {"confidence": 0.9},
                "producer": {"system": "nthlayer-respond"},
            }
        ]))
        client.get_descendants = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=[
            {"id": "vrd-004", "type": "execution"},
        ]))
        client.resolve_outcome = AsyncMock(return_value=APIResult(ok=True, status_code=201, data={}))
        submitted = []
        client.submit_assessment = AsyncMock(
            side_effect=lambda a: (submitted.append(a.get('data', a)), APIResult(ok=True, status_code=201, data={}))[1]
        )

        module = LearnOutcomeModule(client=client)
        await module.process_cycle()

        assert len(submitted) == 1
        cal = submitted[0]
        assert cal["data"]["expressed_confidence"] == 0.9
        assert cal["data"]["observed_outcome"] == "confirmed"
        # delta = |0.9 - 1.0| = 0.1
        assert cal["data"]["calibration_delta"] == 0.1

    async def test_resolution_failure_continues(self):
        """One verdict fails resolution, others still processed."""
        old_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        client = AsyncMock()
        client.get_verdicts = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=[
            {"id": "vrd-a", "created_at": old_time, "outcome": {"status": "pending"}, "judgment": {}, "producer": {}},
            {"id": "vrd-b", "created_at": old_time, "outcome": {"status": "pending"}, "judgment": {}, "producer": {}},
        ]))
        # get_descendants fails for first, succeeds for second
        call_count = {"n": 0}

        async def desc_side_effect(vid):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise Exception("network error")
            return APIResult(ok=True, status_code=200, data=[])

        client.get_descendants = AsyncMock(side_effect=desc_side_effect)
        client.resolve_outcome = AsyncMock()
        client.submit_assessment = AsyncMock()

        module = LearnOutcomeModule(client=client)
        # Should not crash — module continues processing
        await module.process_cycle()

    async def test_resolution_via_divergence(self):
        """Descendant with overridden outcome resolves parent via divergence path."""
        old_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        client = AsyncMock()
        client.get_verdicts = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=[
            {
                "id": "vrd-005",
                "created_at": old_time,
                "type": "approval",
                "service": "svc",
                "outcome": {"status": "pending"},
                "judgment": {"confidence": 0.9},
                "producer": {"system": "nthlayer-respond"},
            }
        ]))
        # Descendant is NOT execution type but has overridden outcome
        client.get_descendants = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=[
            {"id": "vrd-006", "type": "operator_note", "outcome": {"status": "overridden"}},
        ]))
        client.resolve_outcome = AsyncMock(return_value=APIResult(ok=True, status_code=201, data={}))
        submitted = []
        client.submit_assessment = AsyncMock(
            side_effect=lambda a: (submitted.append(a.get('data', a)), APIResult(ok=True, status_code=201, data={}))[1]
        )

        module = LearnOutcomeModule(client=client)
        await module.process_cycle()

        # Should resolve as overridden (divergence path), not confirmed (lineage)
        outcome_arg = client.resolve_outcome.call_args[0][1]
        assert outcome_arg["outcome_status"] == "overridden"
        assert outcome_arg["path"] == "divergence"
        # Calibration delta: |0.9 - 0.0| = 0.9
        assert submitted[0]["data"]["calibration_delta"] == 0.9

    async def test_cursor_advances_after_cycle(self):
        """Outcome module cursor advances after processing verdicts."""
        old_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        client = AsyncMock()
        client.get_verdicts = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=[
            {
                "id": "vrd-cursor",
                "created_at": old_time,
                "type": "approval",
                "service": "svc",
                "outcome": {"status": "pending"},
                "judgment": {},
                "producer": {},
            }
        ]))
        client.get_descendants = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=[]))
        client.resolve_outcome = AsyncMock()
        client.submit_assessment = AsyncMock()

        module = LearnOutcomeModule(client=client)
        await module.process_cycle()

        state = await module.get_state()
        assert "cursor" in state
        assert state["cursor"] == old_time

    async def test_api_failure_no_crash(self):
        client = AsyncMock()
        client.get_verdicts = AsyncMock(
            return_value=APIResult(ok=False, status_code=503, error="unavailable")
        )
        module = LearnOutcomeModule(client=client)
        await module.process_cycle()


# ---------------------------------------------------------------------------
# LearnRetrospectiveModule
# ---------------------------------------------------------------------------


class TestRetrospectiveModuleProtocol:
    def test_name(self):
        module = LearnRetrospectiveModule(client=AsyncMock())
        assert module.name == "learn.retrospective"

    async def test_restore_state_accepts_none(self):
        module = LearnRetrospectiveModule(client=AsyncMock())
        await module.restore_state(None)

    async def test_restore_state_restores_cursor(self):
        module = LearnRetrospectiveModule(client=AsyncMock())
        await module.restore_state({"cursor": "2026-04-24T12:00:00+00:00"})
        assert module._cursor == "2026-04-24T12:00:00+00:00"

    async def test_get_state_includes_cursor(self):
        module = LearnRetrospectiveModule(client=AsyncMock())
        module._cursor = "2026-04-24T12:00:00+00:00"
        state = await module.get_state()
        assert state["cursor"] == "2026-04-24T12:00:00+00:00"

    def test_satisfies_protocol(self):
        from nthlayer_workers.runner import WorkerModule
        module = LearnRetrospectiveModule(client=AsyncMock())
        assert isinstance(module, WorkerModule)


class TestRetrospectiveCycle:
    async def test_snapshot_triggers_retrospective(self):
        """New correlation_snapshot → retrospective assessment emitted."""
        client = AsyncMock()
        client.get_assessments = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=[
            {
                "id": "csn-001",
                "created_at": "2026-04-24T12:00:00+00:00",
                "kind": "correlation_snapshot",
                "service": "fraud-detect",
                "data": {
                    "domain": {"service": "fraud-detect", "environment": "production"},
                    "window": {"duration_seconds": 120, "opened_at": "2026-04-24T11:58:00+00:00", "closed_at": "2026-04-24T12:00:00+00:00"},
                    "affected_services": ["fraud-detect", "payment-api"],
                    "correlation_groups": [],
                },
            }
        ]))
        client.get_verdicts = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=[
            {"id": "vrd-001", "type": "quality_breach", "created_at": "2026-04-24T11:58:00+00:00",
             "service": "fraud-detect", "outcome": {"status": "pending"}},
        ]))
        submitted = []
        client.submit_assessment = AsyncMock(
            side_effect=lambda a: (submitted.append(a.get('data', a)), APIResult(ok=True, status_code=201, data={}))[1]
        )

        module = LearnRetrospectiveModule(client=client)
        await module.process_cycle()

        assert len(submitted) == 1
        retro = submitted[0]
        assert retro["kind"] == "retrospective"
        assert retro["service"] == "fraud-detect"
        assert retro["data"]["verdict_count"] == 1
        assert retro["data"]["outcome_coverage"]["pending"] == 1

    async def test_outcome_coverage_reported(self):
        """Retrospective includes resolved/pending/total counts."""
        client = AsyncMock()
        client.get_assessments = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=[
            {
                "id": "csn-002",
                "created_at": "2026-04-24T12:00:00+00:00",
                "kind": "correlation_snapshot",
                "service": "svc",
                "data": {"domain": {"service": "svc"}, "window": {"duration_seconds": 60, "opened_at": "2026-04-24T11:59:00+00:00", "closed_at": "2026-04-24T12:00:00+00:00"},
                         "affected_services": ["svc"], "correlation_groups": []},
            }
        ]))
        client.get_verdicts = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=[
            {"id": "v1", "type": "approval", "created_at": "2026-04-24T11:59:00+00:00",
             "service": "svc", "outcome": {"status": "confirmed"}},
            {"id": "v2", "type": "action_request", "created_at": "2026-04-24T11:59:30+00:00",
             "service": "svc", "outcome": {"status": "pending"}},
        ]))
        submitted = []
        client.submit_assessment = AsyncMock(
            side_effect=lambda a: (submitted.append(a.get('data', a)), APIResult(ok=True, status_code=201, data={}))[1]
        )

        module = LearnRetrospectiveModule(client=client)
        await module.process_cycle()

        coverage = submitted[0]["data"]["outcome_coverage"]
        assert coverage["resolved"] == 1
        assert coverage["pending"] == 1
        assert coverage["total"] == 2

    async def test_no_snapshots_noop(self):
        client = AsyncMock()
        client.get_assessments = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=[]))
        client.submit_assessment = AsyncMock()

        module = LearnRetrospectiveModule(client=client)
        await module.process_cycle()

        client.submit_assessment.assert_not_awaited()

    async def test_empty_chain_produces_minimal_retrospective(self):
        """Snapshot with no lineage → minimal retrospective with verdict_count: 0."""
        client = AsyncMock()
        client.get_assessments = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=[
            {
                "id": "csn-empty",
                "created_at": "2026-04-24T12:00:00+00:00",
                "kind": "correlation_snapshot",
                "service": "svc",
                "data": {"domain": {"service": "svc"}, "window": {"duration_seconds": 30, "opened_at": "2026-04-24T11:59:30+00:00", "closed_at": "2026-04-24T12:00:00+00:00"},
                         "affected_services": ["svc"], "correlation_groups": []},
            }
        ]))
        client.get_verdicts = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=[]))
        submitted = []
        client.submit_assessment = AsyncMock(
            side_effect=lambda a: (submitted.append(a.get('data', a)), APIResult(ok=True, status_code=201, data={}))[1]
        )

        module = LearnRetrospectiveModule(client=client)
        await module.process_cycle()

        retro = submitted[0]
        assert retro["data"]["verdict_count"] == 0
        assert retro["data"]["timeline"] == []
        assert retro["data"]["outcome_coverage"]["total"] == 0

    async def test_cursor_persisted(self):
        """Cursor advances after processing snapshots."""
        client = AsyncMock()
        client.get_assessments = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=[
            {
                "id": "csn-003",
                "created_at": "2026-04-24T13:00:00+00:00",
                "kind": "correlation_snapshot",
                "service": "svc",
                "data": {"domain": {"service": "svc"}, "window": {}, "affected_services": [],
                         "correlation_groups": []},
            }
        ]))
        client.get_verdicts = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=[]))
        client.submit_assessment = AsyncMock(return_value=APIResult(ok=True, status_code=201, data={}))

        module = LearnRetrospectiveModule(client=client)
        await module.process_cycle()

        state = await module.get_state()
        assert state["cursor"] == "2026-04-24T13:00:00+00:00"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestBuildChainTimeline:
    def test_sorts_chronologically(self):
        chain = [
            {"created_at": "2026-04-24T12:02:00+00:00", "type": "execution", "service": "svc", "id": "v2", "outcome": {}},
            {"created_at": "2026-04-24T12:00:00+00:00", "type": "approval", "service": "svc", "id": "v1", "outcome": {}},
        ]
        timeline = _build_chain_timeline(chain)
        assert timeline[0]["id"] == "v1"
        assert timeline[1]["id"] == "v2"

    def test_empty_chain(self):
        assert _build_chain_timeline([]) == []


class TestGenerateRecommendations:
    def test_slo_gate_on_breach(self):
        chain = [{"type": "quality_breach"}]
        recs = _generate_recommendations(chain, {"affected_services": []})
        assert any(r["type"] == "slo_gate" for r in recs)

    def test_dependency_review_on_large_blast(self):
        recs = _generate_recommendations([], {"affected_services": ["a", "b", "c", "d"]})
        assert any(r["type"] == "dependency_review" for r in recs)

    def test_no_recommendations_for_simple_chain(self):
        recs = _generate_recommendations([{"type": "approval"}], {"affected_services": ["a"]})
        assert recs == []


# ---------------------------------------------------------------------------
# TestRetrospectiveTriggerService — opensrm-dpws
# ---------------------------------------------------------------------------


class TestRetrospectiveTriggerService:
    """opensrm-dpws: worker-path retrospective populates trigger_service
    and (when trigger's manifest is present) declared_dependencies_by_service."""

    async def test_retrospective_includes_trigger_service(self):
        """snapshot.data.domain.service → data['trigger_service']."""
        client = AsyncMock()
        client.get_assessments.return_value = APIResult(
            ok=True, status_code=200,
            data=[{
                "id": "csn-1",
                "service": "fraud-detect",
                "created_at": "2026-05-29T00:00:00+00:00",
                "data": {
                    "domain": {"service": "fraud-detect", "environment": "prod"},
                    "window": {"opened_at": "2026-05-29T00:00:00+00:00",
                               "closed_at": "2026-05-29T00:05:00+00:00",
                               "duration_seconds": 300},
                    "affected_services": ["fraud-detect", "svc-x"],
                },
            }],
        )
        client.get_verdicts.return_value = APIResult(ok=True, status_code=200, data=[])
        client.get_manifests.return_value = APIResult(
            ok=True, status_code=200,
            data=[
                {"name": "fraud-detect", "dependencies": [
                    {"name": "svc-known", "type": "api"},
                ]},
            ],
        )
        client.submit_assessment.return_value = APIResult(ok=True, status_code=200, data={})

        module = LearnRetrospectiveModule(client=client)
        await module.process_cycle()

        submitted = client.submit_assessment.call_args.args[0]
        data = submitted["data"]["data"]
        assert data["trigger_service"] == "fraud-detect"

    async def test_retrospective_trigger_service_fallback_to_top_level_service(self):
        """data.domain.service absent → uses snapshot['service']."""
        client = AsyncMock()
        client.get_assessments.return_value = APIResult(
            ok=True, status_code=200,
            data=[{
                "id": "csn-2",
                "service": "payments",
                "created_at": "2026-05-29T00:00:00+00:00",
                "data": {
                    "domain": {},
                    "window": {"opened_at": "2026-05-29T00:00:00+00:00",
                               "closed_at": "2026-05-29T00:05:00+00:00",
                               "duration_seconds": 300},
                    "affected_services": ["payments"],
                },
            }],
        )
        client.get_verdicts.return_value = APIResult(ok=True, status_code=200, data=[])
        client.get_manifests.return_value = APIResult(
            ok=True, status_code=200,
            data=[{"name": "payments", "dependencies": []}],
        )
        client.submit_assessment.return_value = APIResult(ok=True, status_code=200, data={})

        module = LearnRetrospectiveModule(client=client)
        await module.process_cycle()

        submitted = client.submit_assessment.call_args.args[0]
        data = submitted["data"]["data"]
        assert data["trigger_service"] == "payments"

    async def test_retrospective_includes_declared_dependencies(self):
        """get_manifests returns trigger's manifest → declared_deps populated."""
        client = AsyncMock()
        client.get_assessments.return_value = APIResult(
            ok=True, status_code=200,
            data=[{
                "id": "csn-3", "service": "fraud-detect",
                "created_at": "2026-05-29T00:00:00+00:00",
                "data": {
                    "domain": {"service": "fraud-detect"},
                    "window": {"opened_at": "2026-05-29T00:00:00+00:00",
                               "closed_at": "2026-05-29T00:05:00+00:00",
                               "duration_seconds": 300},
                    "affected_services": ["fraud-detect"],
                },
            }],
        )
        client.get_verdicts.return_value = APIResult(ok=True, status_code=200, data=[])
        client.get_manifests.return_value = APIResult(
            ok=True, status_code=200,
            data=[
                {"name": "fraud-detect", "dependencies": [
                    {"name": "svc-known", "type": "api"},
                ]},
                {"name": "svc-known", "dependencies": []},
            ],
        )
        client.submit_assessment.return_value = APIResult(ok=True, status_code=200, data={})

        module = LearnRetrospectiveModule(client=client)
        await module.process_cycle()

        submitted = client.submit_assessment.call_args.args[0]
        data = submitted["data"]["data"]
        assert data["declared_dependencies_by_service"] == {
            "fraud-detect": ["svc-known"],
            "svc-known": [],
        }

    async def test_retrospective_omits_declared_deps_when_manifest_fetch_fails(self):
        """get_manifests returns ok=False → declared_deps key absent, no crash.
        trigger_service is still populated (decoupled per design § 3.6)."""
        client = AsyncMock()
        client.get_assessments.return_value = APIResult(
            ok=True, status_code=200,
            data=[{
                "id": "csn-4", "service": "fraud-detect",
                "created_at": "2026-05-29T00:00:00+00:00",
                "data": {
                    "domain": {"service": "fraud-detect"},
                    "window": {"opened_at": "2026-05-29T00:00:00+00:00",
                               "closed_at": "2026-05-29T00:05:00+00:00",
                               "duration_seconds": 300},
                    "affected_services": ["fraud-detect"],
                },
            }],
        )
        client.get_verdicts.return_value = APIResult(ok=True, status_code=200, data=[])
        client.get_manifests.return_value = APIResult(
            ok=False, status_code=503, data=None, error="connection_failed",
        )
        client.submit_assessment.return_value = APIResult(ok=True, status_code=200, data={})

        module = LearnRetrospectiveModule(client=client)
        await module.process_cycle()

        submitted = client.submit_assessment.call_args.args[0]
        data = submitted["data"]["data"]
        assert "declared_dependencies_by_service" not in data
        assert data["trigger_service"] == "fraud-detect"

    async def test_retrospective_omits_declared_deps_when_trigger_manifest_absent(self):
        """get_manifests succeeds but trigger's own manifest missing → declared_deps omitted."""
        client = AsyncMock()
        client.get_assessments.return_value = APIResult(
            ok=True, status_code=200,
            data=[{
                "id": "csn-5", "service": "fraud-detect",
                "created_at": "2026-05-29T00:00:00+00:00",
                "data": {
                    "domain": {"service": "fraud-detect"},
                    "window": {"opened_at": "2026-05-29T00:00:00+00:00",
                               "closed_at": "2026-05-29T00:05:00+00:00",
                               "duration_seconds": 300},
                    "affected_services": ["fraud-detect", "svc-x"],
                },
            }],
        )
        client.get_verdicts.return_value = APIResult(ok=True, status_code=200, data=[])
        # Catalogue has svc-x but NOT fraud-detect (the trigger)
        client.get_manifests.return_value = APIResult(
            ok=True, status_code=200,
            data=[{"name": "svc-x", "dependencies": []}],
        )
        client.submit_assessment.return_value = APIResult(ok=True, status_code=200, data={})

        module = LearnRetrospectiveModule(client=client)
        await module.process_cycle()

        submitted = client.submit_assessment.call_args.args[0]
        data = submitted["data"]["data"]
        assert "declared_dependencies_by_service" not in data
        assert data["trigger_service"] == "fraud-detect"
