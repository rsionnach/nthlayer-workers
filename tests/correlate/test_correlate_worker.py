"""Tests for CorrelateSessionModule — session window worker module."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from nthlayer_common.api_client import APIResult
from nthlayer_workers.correlate.worker import (
    CorrelateSessionModule,
    assessment_to_event,
    verdict_to_event,
)
from nthlayer_workers.correlate.types import EventType


class TestCorrelateSessionModuleProtocol:
    def test_name(self):
        module = CorrelateSessionModule(client=AsyncMock())
        assert module.name == "correlate"

    async def test_restore_state_accepts_none(self):
        module = CorrelateSessionModule(client=AsyncMock())
        await module.restore_state(None)

    async def test_restore_state_accepts_empty_dict(self):
        module = CorrelateSessionModule(client=AsyncMock())
        await module.restore_state({})

    async def test_get_state_returns_cursor_and_windows(self):
        module = CorrelateSessionModule(client=AsyncMock())
        state = await module.get_state()
        assert "windows" in state

    def test_satisfies_worker_module_protocol(self):
        from nthlayer_workers.runner import WorkerModule

        module = CorrelateSessionModule(client=AsyncMock())
        assert isinstance(module, WorkerModule)


class TestProcessCycle:
    async def test_no_events_no_crash(self):
        client = AsyncMock()
        client.get_verdicts = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=[]))
        client.get_assessments = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=[]))
        client.submit_assessment = AsyncMock()

        module = CorrelateSessionModule(client=client)
        await module.process_cycle()

        client.submit_assessment.assert_not_awaited()

    async def test_events_open_window_no_immediate_close(self):
        """Events arrive but gap hasn't elapsed — no snapshot yet."""
        now = datetime.now(timezone.utc)
        client = AsyncMock()
        client.get_verdicts = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=[]))
        client.get_assessments = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=[
            {
                "id": "asm-001",
                "created_at": now.isoformat(),
                "kind": "slo_status",
                "service": "fraud-detect",
                "data": {"status": "CRITICAL"},
            }
        ]))
        client.submit_assessment = AsyncMock()

        module = CorrelateSessionModule(client=client, gap_seconds=60.0)
        await module.process_cycle()

        # Window opened but not closed — no snapshot
        client.submit_assessment.assert_not_awaited()
        assert len(module._manager.open_windows) == 1

    async def test_trigger_verdict_force_closes_window(self):
        """quality_breach verdict closes window immediately."""
        now = datetime.now(timezone.utc)
        breach = {
            "id": "vrd-001",
            "created_at": now.isoformat(),
            "type": "quality_breach",
            "service": "fraud-detect",
        }
        client = AsyncMock()

        async def verdict_side_effect(*, verdict_type=None, **_):
            if verdict_type == "quality_breach":
                return APIResult(ok=True, status_code=200, data=[breach])
            return APIResult(ok=True, status_code=200, data=[])

        client.get_verdicts = AsyncMock(side_effect=verdict_side_effect)
        client.get_assessments = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=[]))
        client.submit_assessment = AsyncMock(return_value=APIResult(ok=True, status_code=201, data={"id": "csn-001"}))

        module = CorrelateSessionModule(client=client)
        await module.process_cycle()

        # Window force-closed by trigger, snapshot emitted
        client.submit_assessment.assert_awaited_once()
        submitted = client.submit_assessment.call_args[0][0]
        assert submitted["kind"] == "correlation_snapshot"
        assert submitted["service"] == "fraud-detect"
        assert "nl_summary" in submitted["data"]  # P3-D.3 wiring

    async def test_gap_timeout_closes_window(self):
        """Window closes when gap timeout elapses across cycles."""
        old_time = datetime.now(timezone.utc) - timedelta(seconds=120)
        client = AsyncMock()

        # First cycle: events arrive
        client.get_verdicts = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=[]))
        client.get_assessments = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=[
            {
                "id": "asm-001",
                "created_at": old_time.isoformat(),
                "kind": "slo_status",
                "service": "fraud-detect",
                "data": {"status": "WARNING"},
            }
        ]))
        client.submit_assessment = AsyncMock(return_value=APIResult(ok=True, status_code=201, data={"id": "csn-001"}))

        module = CorrelateSessionModule(client=client, gap_seconds=60.0)
        await module.process_cycle()
        assert len(module._manager.open_windows) == 0  # Gap already exceeded

        # Should have closed and emitted
        client.submit_assessment.assert_awaited_once()

    async def test_state_persistence_roundtrip(self):
        """Cursor and window state persist and restore."""
        now = datetime.now(timezone.utc)
        client = AsyncMock()
        client.get_verdicts = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=[]))
        client.get_assessments = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=[
            {
                "id": "asm-001",
                "created_at": now.isoformat(),
                "kind": "slo_status",
                "service": "payment-api",
                "data": {"status": "HEALTHY"},
            }
        ]))
        client.submit_assessment = AsyncMock()

        module = CorrelateSessionModule(client=client, gap_seconds=60.0)
        await module.process_cycle()

        state = await module.get_state()
        assert state.get("cursor") is not None
        assert "payment-api:production" in state["windows"]

        # Restore into new module
        module2 = CorrelateSessionModule(client=client)
        await module2.restore_state(state)
        assert module2._cursor == state["cursor"]
        assert len(module2._manager.open_windows) == 1

    async def test_api_failure_no_crash(self):
        client = AsyncMock()
        client.get_verdicts = AsyncMock(return_value=APIResult(ok=False, status_code=503, error="unavailable"))
        client.get_assessments = AsyncMock(return_value=APIResult(ok=False, status_code=503, error="unavailable"))
        client.submit_assessment = AsyncMock()

        module = CorrelateSessionModule(client=client)
        await module.process_cycle()  # should not raise

        client.submit_assessment.assert_not_awaited()


class TestEventConversion:
    def test_verdict_to_event_quality_breach(self):
        v = {"id": "vrd-001", "created_at": "2026-04-24T12:00:00+00:00", "type": "quality_breach", "service": "svc"}
        e = verdict_to_event(v)
        assert e.type == EventType.QUALITY_SCORE
        assert e.service == "svc"
        assert e.severity == 0.9

    def test_verdict_to_event_other_type(self):
        v = {"id": "vrd-002", "created_at": "2026-04-24T12:00:00+00:00", "type": "autonomy_change", "service": "svc"}
        e = verdict_to_event(v)
        assert e.type == EventType.VERDICT
        assert e.severity == 0.5

    def test_assessment_to_event_breach(self):
        a = {"id": "asm-001", "created_at": "2026-04-24T12:00:00+00:00", "kind": "slo_status", "service": "svc", "data": {"status": "EXHAUSTED"}}
        e = assessment_to_event(a)
        assert e.type == EventType.METRIC_BREACH
        assert e.severity == 1.0

    def test_assessment_to_event_healthy(self):
        a = {"id": "asm-002", "created_at": "2026-04-24T12:00:00+00:00", "kind": "slo_status", "service": "svc", "data": {"status": "HEALTHY"}}
        e = assessment_to_event(a)
        assert e.type == EventType.ALERT
        assert e.severity == 0.1

    def test_assessment_environment_defaults_to_production(self):
        a = {"id": "asm-003", "created_at": "2026-04-24T12:00:00+00:00", "kind": "slo_status", "service": "svc", "data": {}}
        e = assessment_to_event(a)
        assert e.environment == "production"
