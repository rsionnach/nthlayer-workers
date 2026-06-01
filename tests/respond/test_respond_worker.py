"""Tests for RespondModule worker (P3-E.1)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from nthlayer_workers.respond.config import RespondConfig
from nthlayer_workers.respond.types import (
    IncidentContext,
    IncidentState,
)
from nthlayer_workers.respond.worker import (
    Cursors,
    RespondModule,
    _is_terminal_and_aged,
    _NoopContextStore,
)
from nthlayer_workers.respond.worker_helpers import (
    open_from_breach,
    open_from_snapshot,
)
from nthlayer_workers.runner import WorkerModule

# ------------------------------------------------------------------ #
# Fixtures                                                             #
# ------------------------------------------------------------------ #


@pytest.fixture
def fake_client():
    return AsyncMock()


@pytest.fixture
def config():
    # Smaller defaults to keep tests fast; production defaults validated in
    # test_config.py.
    return RespondConfig(
        cycle_interval_seconds=1.0,
        fallback_threshold_seconds=1.0,
        terminal_retention_seconds=10.0,
        step_timeout_seconds=1.0,
    )


def make_context(
    *,
    incident_id: str = "INC-TEST-001",
    state: IncidentState = IncidentState.TRIAGING,
    updated_at: str | None = None,
    verdict_chain: list[str] | None = None,
) -> IncidentContext:
    now = datetime.now(UTC).isoformat()
    return IncidentContext(
        id=incident_id,
        state=state,
        created_at=now,
        updated_at=updated_at or now,
        trigger_source="nthlayer-correlate",
        trigger_verdict_ids=["asm-snap-1"],
        topology={},
        verdict_chain=list(verdict_chain) if verdict_chain else [],
    )


# ------------------------------------------------------------------ #
# Module protocol                                                      #
# ------------------------------------------------------------------ #


def test_respond_module_satisfies_worker_protocol(fake_client, config):
    module = RespondModule(client=fake_client, config=config)
    assert isinstance(module, WorkerModule)
    assert module.name == "respond"


async def test_restore_state_none_leaves_defaults(fake_client, config):
    module = RespondModule(client=fake_client, config=config)
    await module.restore_state(None)
    assert module._cursors.snapshot_after is None
    assert module._cursors.breach_after is None
    assert module._incidents == {}


async def test_restore_state_empty_dict(fake_client, config):
    module = RespondModule(client=fake_client, config=config)
    await module.restore_state({})
    assert module._cursors.snapshot_after is None
    assert module._incidents == {}


# ------------------------------------------------------------------ #
# State persistence                                                    #
# ------------------------------------------------------------------ #


async def test_get_state_serializes_cursors_and_incidents(fake_client, config):
    module = RespondModule(client=fake_client, config=config)
    module._cursors = Cursors(snapshot_after="2026-04-25T12:00:00+00:00",
                              breach_after="2026-04-25T11:59:00+00:00")
    ctx = make_context(state=IncidentState.TRIAGING, verdict_chain=["vrd-1", "vrd-2"])
    module._incidents[ctx.id] = ctx

    state = await module.get_state()
    assert state["cursors"]["snapshot_after"] == "2026-04-25T12:00:00+00:00"
    assert state["cursors"]["breach_after"] == "2026-04-25T11:59:00+00:00"
    assert ctx.id in state["incidents"]


async def test_get_state_keeps_active_incidents_regardless_of_age(fake_client, config):
    """Active (non-terminal) incidents are never pruned."""
    module = RespondModule(client=fake_client, config=config)
    old_time = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    ctx = make_context(state=IncidentState.TRIAGING, updated_at=old_time)
    module._incidents[ctx.id] = ctx

    state = await module.get_state()
    assert ctx.id in state["incidents"]


async def test_get_state_prunes_terminal_aged_incidents(fake_client, config):
    """Terminal incidents older than terminal_retention_seconds are pruned."""
    module = RespondModule(client=fake_client, config=config)
    # config.terminal_retention_seconds is 10s in the test config
    too_old = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
    fresh = datetime.now(UTC).isoformat()

    aged = make_context(
        incident_id="INC-AGED",
        state=IncidentState.RESOLVED,
        updated_at=too_old,
    )
    recent = make_context(
        incident_id="INC-RECENT",
        state=IncidentState.RESOLVED,
        updated_at=fresh,
    )
    module._incidents[aged.id] = aged
    module._incidents[recent.id] = recent

    state = await module.get_state()
    assert "INC-AGED" not in state["incidents"]
    assert "INC-RECENT" in state["incidents"]


async def test_state_roundtrip_preserves_all_fields(fake_client, config):
    """get_state → restore_state on a fresh module restores cursors and incidents.

    P3-E.1 critical invariant: verdict_chain MUST survive roundtrip. Without
    it, post-restart _emit_verdict reads context.verdict_chain[-1] against an
    empty list and produces verdicts with parent=None and broken parent_ids,
    breaking lineage continuity across restarts.
    """
    src = RespondModule(client=fake_client, config=config)
    src._cursors = Cursors(snapshot_after="2026-04-25T10:00:00+00:00")
    ctx = make_context(
        incident_id="INC-ROUND",
        state=IncidentState.INVESTIGATING,
        verdict_chain=["vrd-triage-1", "vrd-investigation-1"],
    )
    ctx.metadata = {"escalation_pending": False, "blast_radius": ["a", "b"]}
    src._incidents[ctx.id] = ctx

    state = await src.get_state()

    dst = RespondModule(client=fake_client, config=config)
    await dst.restore_state(state)

    assert dst._cursors.snapshot_after == "2026-04-25T10:00:00+00:00"
    assert "INC-ROUND" in dst._incidents
    restored = dst._incidents["INC-ROUND"]
    assert restored.state == IncidentState.INVESTIGATING
    # CRITICAL: verdict_chain preserved
    assert restored.verdict_chain == ["vrd-triage-1", "vrd-investigation-1"]
    assert restored.metadata.get("blast_radius") == ["a", "b"]


async def test_restore_state_skips_corrupt_incident(fake_client, config):
    module = RespondModule(client=fake_client, config=config)
    state = {
        "cursors": {"snapshot_after": "2026-04-25T10:00:00Z"},
        "incidents": {
            "INC-BAD": {"this_is_not_a_valid": "incident_dict"},
        },
    }
    # Should not raise; corrupt incident silently skipped, cursor still restored
    await module.restore_state(state)
    assert "INC-BAD" not in module._incidents
    assert module._cursors.snapshot_after == "2026-04-25T10:00:00Z"


# ------------------------------------------------------------------ #
# _NoopContextStore                                                    #
# ------------------------------------------------------------------ #


def test_noop_context_store_save_is_silent():
    store = _NoopContextStore()
    ctx = make_context()
    store.save(ctx)  # no exception


def test_noop_context_store_load_raises():
    store = _NoopContextStore()
    with pytest.raises(NotImplementedError, match="worker mode reads from"):
        store.load("INC-1")


def test_noop_context_store_list_active_raises():
    store = _NoopContextStore()
    with pytest.raises(NotImplementedError, match="worker mode reads from"):
        store.list_active()


# ------------------------------------------------------------------ #
# _is_terminal_and_aged                                                #
# ------------------------------------------------------------------ #


def test_is_terminal_and_aged_active_returns_false():
    ctx = make_context(state=IncidentState.TRIAGING)
    assert _is_terminal_and_aged(ctx, 10.0) is False


def test_is_terminal_and_aged_terminal_recent_returns_false():
    ctx = make_context(state=IncidentState.RESOLVED)
    assert _is_terminal_and_aged(ctx, 10.0) is False


def test_is_terminal_and_aged_terminal_aged_returns_true():
    old = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    ctx = make_context(state=IncidentState.RESOLVED, updated_at=old)
    assert _is_terminal_and_aged(ctx, 10.0) is True


def test_is_terminal_and_aged_malformed_timestamp_returns_false():
    """Malformed timestamps don't accidentally drop the incident."""
    ctx = make_context(state=IncidentState.RESOLVED, updated_at="not-a-timestamp")
    assert _is_terminal_and_aged(ctx, 10.0) is False


def test_is_terminal_and_aged_naive_datetime_handled():
    """R5 edge case fix: a naive (no-timezone) updated_at must not crash with
    TypeError when subtracted from now(tz=utc). Worker treats naive as UTC."""
    naive_old = (datetime.now(UTC) - timedelta(hours=1)).replace(tzinfo=None).isoformat()
    ctx = make_context(state=IncidentState.RESOLVED, updated_at=naive_old)
    # Should successfully classify as aged (1h > 10s retention)
    assert _is_terminal_and_aged(ctx, 10.0) is True


# ------------------------------------------------------------------ #
# Trigger ingestion: primary path (snapshots) — step 7                 #
# ------------------------------------------------------------------ #


def _ok_result(data):
    from nthlayer_common.api_client import APIResult
    return APIResult(ok=True, status_code=200, data=data, error=None, detail=None)


def _err_result(status: int = 503):
    from nthlayer_common.api_client import APIResult
    return APIResult(
        ok=False, status_code=status, data=None, error="server_error", detail=None,
    )


def _snapshot(snap_id="asm-snap-1", created_at="2026-04-25T12:00:00+00:00",
              service="fraud-detect", parent_ids=None, affected=None,
              blast_radius=None, peak_severity=0.9, event_count=5,
              nl_summary="summary"):
    return {
        "id": snap_id,
        "created_at": created_at,
        "kind": "correlation_snapshot",
        "service": service,
        "data": {
            "domain": {"service": service, "environment": "production"},
            "parent_ids": parent_ids or [],
            "affected_services": affected or [service],
            "blast_radius": blast_radius or [],
            "peak_severity": peak_severity,
            "event_count": event_count,
            "nl_summary": nl_summary,
        },
    }


def _breach(breach_id="vrd-breach-1", created_at="2026-04-25T11:00:00+00:00",
            service="fraud-detect", severity="critical"):
    return {
        "id": breach_id,
        "created_at": created_at,
        "type": "quality_breach",
        "service": service,
        "severity": severity,
    }


async def test_ingest_snapshot_opens_incident(fake_client, config):
    snap = _snapshot(parent_ids=["vrd-breach-x"])
    fake_client.get_assessments = AsyncMock(return_value=_ok_result([snap]))
    fake_client.get_verdicts = AsyncMock(return_value=_ok_result([]))

    module = RespondModule(client=fake_client, config=config)
    await module.process_cycle()

    assert len(module._incidents) == 1
    inc = next(iter(module._incidents.values()))
    assert inc.trigger_source == "nthlayer-correlate"
    # trigger_verdict_ids includes snapshot id + parent breach id
    assert "asm-snap-1" in inc.trigger_verdict_ids
    assert "vrd-breach-x" in inc.trigger_verdict_ids


async def test_ingest_snapshot_metadata_populated(fake_client, config):
    snap = _snapshot(
        blast_radius=["payment-api", "checkout-svc"],
        affected=["fraud-detect", "payment-api"],
    )
    fake_client.get_assessments = AsyncMock(return_value=_ok_result([snap]))
    fake_client.get_verdicts = AsyncMock(return_value=_ok_result([]))

    module = RespondModule(client=fake_client, config=config)
    await module.process_cycle()

    inc = next(iter(module._incidents.values()))
    assert inc.metadata["blast_radius"] == ["payment-api", "checkout-svc"]
    assert inc.metadata["correlation_summary"] == "summary"
    assert inc.metadata["peak_severity"] == 0.9
    assert inc.metadata["event_count"] == 5
    assert inc.metadata["affected_services"] == ["fraud-detect", "payment-api"]


async def test_ingest_snapshot_cursor_advances(fake_client, config):
    snaps = [
        _snapshot(snap_id="asm-1", created_at="2026-04-25T11:00:00+00:00"),
        _snapshot(snap_id="asm-2", created_at="2026-04-25T12:00:00+00:00"),
    ]
    fake_client.get_assessments = AsyncMock(return_value=_ok_result(snaps))
    fake_client.get_verdicts = AsyncMock(return_value=_ok_result([]))

    module = RespondModule(client=fake_client, config=config)
    await module.process_cycle()

    assert module._cursors.snapshot_after == "2026-04-25T12:00:00+00:00"
    assert len(module._incidents) == 2


async def test_ingest_no_new_snapshots_noop(fake_client, config):
    fake_client.get_assessments = AsyncMock(return_value=_ok_result([]))
    fake_client.get_verdicts = AsyncMock(return_value=_ok_result([]))

    module = RespondModule(client=fake_client, config=config)
    await module.process_cycle()

    assert module._incidents == {}
    assert module._cursors.snapshot_after is None


async def test_ingest_already_triggered_snapshot_skipped(fake_client, config):
    """Defence against cursor hiccups: don't re-open an already-triggered snapshot."""
    snap = _snapshot(snap_id="asm-dup")
    fake_client.get_assessments = AsyncMock(return_value=_ok_result([snap]))
    fake_client.get_verdicts = AsyncMock(return_value=_ok_result([]))

    module = RespondModule(client=fake_client, config=config)
    # Simulate a prior cycle having opened an incident from this snapshot
    existing = make_context(incident_id="INC-PREEXISTING")
    existing.trigger_verdict_ids = ["asm-dup"]
    module._incidents[existing.id] = existing

    await module.process_cycle()
    # Still only one incident — the pre-existing one
    assert len(module._incidents) == 1
    assert "INC-PREEXISTING" in module._incidents


async def test_ingest_snapshot_poll_failure_no_crash(fake_client, config):
    fake_client.get_assessments = AsyncMock(return_value=_err_result(503))
    fake_client.get_verdicts = AsyncMock(return_value=_ok_result([]))

    module = RespondModule(client=fake_client, config=config)
    await module.process_cycle()  # no exception
    assert module._incidents == {}


# ------------------------------------------------------------------ #
# Trigger ingestion: fallback path (orphan breaches) — step 8          #
# ------------------------------------------------------------------ #


async def test_ingest_orphan_breach_opens_fallback_incident(fake_client, config):
    breach = _breach(breach_id="vrd-orphan")
    # First call (kind=correlation_snapshot in _ingest_snapshots): empty.
    # Second call (kind=correlation_snapshot in _has_associated_snapshot): empty
    # → no associated snapshot → fallback fires.
    fake_client.get_assessments = AsyncMock(return_value=_ok_result([]))
    fake_client.get_verdicts = AsyncMock(return_value=_ok_result([breach]))

    module = RespondModule(client=fake_client, config=config)
    await module.process_cycle()

    assert len(module._incidents) == 1
    inc = next(iter(module._incidents.values()))
    assert inc.trigger_source == "nthlayer-measure-fallback"
    assert inc.trigger_verdict_ids == ["vrd-orphan"]
    assert inc.metadata["fallback_reason"] == "no_correlation_snapshot"
    assert inc.metadata["severity"] == "critical"


async def test_ingest_breach_with_snapshot_skipped(fake_client, config):
    """Breach already represented by a snapshot must not produce a fallback incident."""
    breach = _breach(breach_id="vrd-covered")
    snap = _snapshot(snap_id="asm-covers-breach", parent_ids=["vrd-covered"])

    # First call returns snap (used by both _ingest_snapshots cursor scan AND
    # _has_associated_snapshot lookup). Snap will open one incident on the
    # primary path, and the fallback dedup check finds it referenced → no
    # second incident.
    fake_client.get_assessments = AsyncMock(return_value=_ok_result([snap]))
    fake_client.get_verdicts = AsyncMock(return_value=_ok_result([breach]))

    module = RespondModule(client=fake_client, config=config)
    await module.process_cycle()

    # Exactly one incident — from the snapshot. The breach was covered.
    assert len(module._incidents) == 1
    inc = next(iter(module._incidents.values()))
    assert inc.trigger_source == "nthlayer-correlate"


async def test_ingest_breach_already_triggered_skipped(fake_client, config):
    """Cursor hiccup defence on the breach path."""
    breach = _breach(breach_id="vrd-dup")
    fake_client.get_assessments = AsyncMock(return_value=_ok_result([]))
    fake_client.get_verdicts = AsyncMock(return_value=_ok_result([breach]))

    module = RespondModule(client=fake_client, config=config)
    existing = make_context(incident_id="INC-PRE")
    existing.trigger_verdict_ids = ["vrd-dup"]
    module._incidents[existing.id] = existing

    await module.process_cycle()
    assert len(module._incidents) == 1


async def test_ingest_breach_poll_failure_no_crash(fake_client, config):
    fake_client.get_assessments = AsyncMock(return_value=_ok_result([]))
    fake_client.get_verdicts = AsyncMock(return_value=_err_result(503))

    module = RespondModule(client=fake_client, config=config)
    await module.process_cycle()
    assert module._incidents == {}


# ------------------------------------------------------------------ #
# Drive active incidents — step 9                                      #
# ------------------------------------------------------------------ #


async def test_drive_skips_terminal_incidents(fake_client, config):
    """Incidents in RESOLVED/ESCALATED/FAILED are not driven through pipeline."""
    fake_client.get_assessments = AsyncMock(return_value=_ok_result([]))
    fake_client.get_verdicts = AsyncMock(return_value=_ok_result([]))

    module = RespondModule(client=fake_client, config=config)

    # Inject a mock coordinator to verify it's NOT called for terminal incidents
    mock_coord = AsyncMock()
    mock_coord.run = AsyncMock()
    module._coordinator = mock_coord

    for state in (IncidentState.RESOLVED, IncidentState.ESCALATED, IncidentState.FAILED):
        ctx = make_context(incident_id=f"INC-{state.value}", state=state)
        module._incidents[ctx.id] = ctx

    await module.process_cycle()
    mock_coord.run.assert_not_awaited()


async def test_drive_skips_awaiting_approval(fake_client, config):
    """AWAITING_APPROVAL incidents wait for P3-E.3; coordinator not called."""
    fake_client.get_assessments = AsyncMock(return_value=_ok_result([]))
    fake_client.get_verdicts = AsyncMock(return_value=_ok_result([]))

    module = RespondModule(client=fake_client, config=config)
    mock_coord = AsyncMock()
    mock_coord.run = AsyncMock()
    module._coordinator = mock_coord

    ctx = make_context(state=IncidentState.AWAITING_APPROVAL)
    module._incidents[ctx.id] = ctx

    await module.process_cycle()
    mock_coord.run.assert_not_awaited()


async def test_drive_progresses_active_incident(fake_client, config):
    """Non-terminal, non-AWAITING_APPROVAL incident is driven through coordinator."""
    fake_client.get_assessments = AsyncMock(return_value=_ok_result([]))
    fake_client.get_verdicts = AsyncMock(return_value=_ok_result([]))

    module = RespondModule(client=fake_client, config=config)

    ctx = make_context(state=IncidentState.TRIAGING)
    module._incidents[ctx.id] = ctx

    # Mock coordinator that flips the incident to RESOLVED
    async def fake_run(c):
        c.state = IncidentState.RESOLVED
        return c

    mock_coord = AsyncMock()
    mock_coord.run = AsyncMock(side_effect=fake_run)
    module._coordinator = mock_coord

    await module.process_cycle()
    mock_coord.run.assert_awaited_once()
    assert module._incidents[ctx.id].state == IncidentState.RESOLVED


async def test_drive_one_incident_failure_does_not_block_others(fake_client, config):
    """An exception escaping coordinator.run for incident A does not prevent
    incident B from being driven.
    """
    fake_client.get_assessments = AsyncMock(return_value=_ok_result([]))
    fake_client.get_verdicts = AsyncMock(return_value=_ok_result([]))

    module = RespondModule(client=fake_client, config=config)

    ctx_a = make_context(incident_id="INC-A", state=IncidentState.TRIAGING)
    ctx_b = make_context(incident_id="INC-B", state=IncidentState.TRIAGING)
    module._incidents["INC-A"] = ctx_a
    module._incidents["INC-B"] = ctx_b

    drive_calls: list[str] = []

    async def fake_run(c):
        drive_calls.append(c.id)
        if c.id == "INC-A":
            raise RuntimeError("simulated escape")
        c.state = IncidentState.RESOLVED
        return c

    mock_coord = AsyncMock()
    mock_coord.run = AsyncMock(side_effect=fake_run)
    module._coordinator = mock_coord

    await module.process_cycle()

    # Both incidents were driven
    assert "INC-A" in drive_calls
    assert "INC-B" in drive_calls
    # A is FAILED with the escape reason
    assert module._incidents["INC-A"].state == IncidentState.FAILED
    assert module._incidents["INC-A"].error.startswith("unrecoverable: ")
    # B progressed
    assert module._incidents["INC-B"].state == IncidentState.RESOLVED


# ------------------------------------------------------------------ #
# Incident opening helpers — step 10 (open_from_snapshot/breach)        #
# ------------------------------------------------------------------ #


def test_open_from_snapshot_id_format():
    from nthlayer_workers.respond.worker_helpers import open_from_snapshot

    cfg = RespondConfig(escalation_threshold=0.3)
    snap = _snapshot(service="payment-api")
    ctx = open_from_snapshot(snap, cfg)
    assert ctx.id.startswith("INC-PAYMENT-API-")
    # Format: INC-<SVC>-YYYYMMDD-HHMMSS-<uuid8>
    assert ctx.id.count("-") >= 4


def test_open_from_snapshot_captures_escalation_threshold():
    """Capture-at-write-time: threshold stored on context.metadata so the
    escalation gate doesn't need a config back-reference."""
    from nthlayer_workers.respond.worker_helpers import open_from_snapshot

    cfg = RespondConfig(escalation_threshold=0.42)
    snap = _snapshot()
    ctx = open_from_snapshot(snap, cfg)
    assert ctx.metadata["escalation_threshold"] == 0.42


def test_open_from_breach_captures_escalation_threshold():
    from nthlayer_workers.respond.worker_helpers import open_from_breach

    cfg = RespondConfig(escalation_threshold=0.42)
    breach = _breach()
    ctx = open_from_breach(breach, cfg)
    assert ctx.metadata["escalation_threshold"] == 0.42


def test_open_from_breach_static_fallback_reason():
    """fallback_reason is a static string — threshold is logged separately
    so the metadata stays stable if the threshold becomes configurable."""
    from nthlayer_workers.respond.worker_helpers import open_from_breach

    cfg = RespondConfig(fallback_threshold_seconds=60.0)
    ctx = open_from_breach(_breach(), cfg)
    assert ctx.metadata["fallback_reason"] == "no_correlation_snapshot"
    # Threshold is NOT in the metadata string (would drift if config changes)
    assert "60" not in ctx.metadata["fallback_reason"]


# ------------------------------------------------------------------ #
# Lineage continuity                                                   #
# ------------------------------------------------------------------ #


async def test_first_verdict_from_snapshot_parents_to_trigger(fake_client, config):
    """Lineage bridge: first respond verdict's parent_ids points to upstream
    trigger (snapshot id + originating breach ids)."""
    from nthlayer_common.api_client import APIResult

    from nthlayer_workers.respond.agents.base import AgentBase
    from nthlayer_workers.respond.types import AgentRole, TriageResult

    class StubAgent(AgentBase):
        role = AgentRole.TRIAGE
        def build_prompt(self, ctx): return ("s", "u")
        def parse_response(self, r, ctx): return TriageResult(0, [], [], None, "x")
        def _apply_result(self, ctx, r):
            ctx.triage = r
            return ctx

    fake_client.submit_verdict = AsyncMock(
        return_value=APIResult(ok=True, status_code=200, data={}, error=None, detail=None)
    )

    agent = StubAgent(model="m", max_tokens=10, client=fake_client, config={})
    snap = _snapshot(parent_ids=["vrd-breach-up"])
    ctx = open_from_snapshot(snap, config)

    v = await agent._emit_verdict(ctx, "s", "flag", 0.5, "r")
    # P3-E.1: first verdict's parent_ids points to upstream triggers
    assert v.parent_ids == ["asm-snap-1", "vrd-breach-up"]
    assert v.lineage.parent is None
    assert v.lineage.context == ["asm-snap-1", "vrd-breach-up"]


async def test_chained_verdicts_parent_to_previous(fake_client, config):
    """Subsequent respond verdicts chain to the previous respond verdict via
    parent_ids and lineage.parent."""
    from nthlayer_common.api_client import APIResult

    from nthlayer_workers.respond.agents.base import AgentBase
    from nthlayer_workers.respond.types import AgentRole, TriageResult

    class StubAgent(AgentBase):
        role = AgentRole.TRIAGE
        def build_prompt(self, ctx): return ("s", "u")
        def parse_response(self, r, ctx): return TriageResult(0, [], [], None, "x")
        def _apply_result(self, ctx, r):
            ctx.triage = r
            return ctx

    fake_client.submit_verdict = AsyncMock(
        return_value=APIResult(ok=True, status_code=200, data={}, error=None, detail=None)
    )
    agent = StubAgent(model="m", max_tokens=10, client=fake_client, config={})
    snap = _snapshot()
    ctx = open_from_snapshot(snap, config)

    v1 = await agent._emit_verdict(ctx, "first", "flag", 0.5, "r")
    v2 = await agent._emit_verdict(ctx, "second", "flag", 0.6, "r")
    # First references trigger
    assert v1.parent_ids == ["asm-snap-1"]
    assert v1.lineage.parent is None
    # Second references first
    assert v2.parent_ids == [v1.id]
    assert v2.lineage.parent == v1.id


async def test_fallback_path_lineage_walk(fake_client, config):
    """Fallback lineage walk: opening from a breach yields the same chaining
    rules as opening from a snapshot. Walking parent_ids from a final verdict
    leads back through earlier respond verdicts to the originating breach."""
    from nthlayer_common.api_client import APIResult

    from nthlayer_workers.respond.agents.base import AgentBase
    from nthlayer_workers.respond.types import AgentRole, TriageResult

    class StubAgent(AgentBase):
        role = AgentRole.TRIAGE
        def build_prompt(self, ctx): return ("s", "u")
        def parse_response(self, r, ctx): return TriageResult(0, [], [], None, "x")
        def _apply_result(self, ctx, r):
            ctx.triage = r
            return ctx

    submitted: list[dict] = []

    async def capture_submit(payload):
        submitted.append(payload)
        return APIResult(ok=True, status_code=200, data={}, error=None, detail=None)

    fake_client.submit_verdict = AsyncMock(side_effect=capture_submit)

    agent = StubAgent(model="m", max_tokens=10, client=fake_client, config={})

    # Open via fallback path (breach without snapshot)
    breach = _breach(breach_id="vrd-orphan-breach")
    ctx = open_from_breach(breach, config)

    # Run pipeline-like sequence: 4 verdicts (triage → investigation → comm → remediation)
    v1 = await agent._emit_verdict(ctx, "triage", "flag", 0.5, "r")
    v2 = await agent._emit_verdict(ctx, "investigation", "flag", 0.6, "r")
    v3 = await agent._emit_verdict(ctx, "communication", "flag", 0.5, "r")
    v4 = await agent._emit_verdict(ctx, "remediation", "flag", 0.5, "r")

    # Walk parent_ids back through the chain
    walked = [v4.id]
    current_parents = v4.parent_ids
    while current_parents:
        # Take immediate predecessor (single-parent chain in respond)
        parent = current_parents[0]
        walked.append(parent)
        # Find the verdict object in our local set
        match = next((v for v in (v1, v2, v3, v4) if v.id == parent), None)
        if match is None:
            break
        current_parents = match.parent_ids

    # walked should be: [v4, v3, v2, v1, vrd-orphan-breach]
    expected = [v4.id, v3.id, v2.id, v1.id, "vrd-orphan-breach"]
    assert walked == expected, f"lineage walk: {walked!r}, expected {expected!r}"


async def test_open_from_snapshot_resolves_tier_from_manifest(fake_client, config):
    """R5 correctness fix: opened incidents must reflect real service tier
    from the manifest, not a hardcoded 'standard' placeholder."""
    snap = _snapshot(service="payment-api", affected=["payment-api"])
    fake_client.get_assessments = AsyncMock(return_value=_ok_result([snap]))
    fake_client.get_verdicts = AsyncMock(return_value=_ok_result([]))
    fake_client.get_manifest = AsyncMock(
        return_value=_ok_result({"name": "payment-api", "tier": "critical"})
    )

    module = RespondModule(client=fake_client, config=config)
    # Drive only the ingest phase; bypass coordinator (no agents needed)
    await module._ingest_triggers()

    inc = next(iter(module._incidents.values()))
    services = inc.topology["services"]
    assert services[0]["name"] == "payment-api"
    assert services[0]["tier"] == "critical"


async def test_open_from_breach_resolves_tier_from_manifest(fake_client, config):
    breach = _breach(service="fraud-detect")
    fake_client.get_assessments = AsyncMock(return_value=_ok_result([]))
    fake_client.get_verdicts = AsyncMock(return_value=_ok_result([breach]))
    fake_client.get_manifest = AsyncMock(
        return_value=_ok_result({"name": "fraud-detect", "tier": "critical"})
    )

    module = RespondModule(client=fake_client, config=config)
    await module._ingest_triggers()

    inc = next(iter(module._incidents.values()))
    assert inc.topology["services"][0]["tier"] == "critical"


async def test_open_falls_back_to_standard_when_manifest_missing(fake_client, config):
    """If the manifest fetch fails (404, server error), tier falls back to
    'standard' so the incident still opens — degraded but not blocked."""
    snap = _snapshot(service="unknown-svc", affected=["unknown-svc"])
    fake_client.get_assessments = AsyncMock(return_value=_ok_result([snap]))
    fake_client.get_verdicts = AsyncMock(return_value=_ok_result([]))
    fake_client.get_manifest = AsyncMock(return_value=_err_result(404))

    module = RespondModule(client=fake_client, config=config)
    await module._ingest_triggers()

    inc = next(iter(module._incidents.values()))
    assert inc.topology["services"][0]["tier"] == "standard"


async def test_resolve_tiers_degrades_on_manifest_fetch_exception(fake_client, config):
    """R5 edge case fix: get_manifest raising must not drop the trigger.

    A network blip during tier lookup degrades to 'standard' rather than
    propagating an exception that would be caught by the open-incident
    try/except and silently drop the trigger.
    """
    snap = _snapshot(service="payment-api", affected=["payment-api"])
    fake_client.get_assessments = AsyncMock(return_value=_ok_result([snap]))
    fake_client.get_verdicts = AsyncMock(return_value=_ok_result([]))
    fake_client.get_manifest = AsyncMock(side_effect=RuntimeError("transport boom"))

    module = RespondModule(client=fake_client, config=config)
    await module._ingest_triggers()

    # Incident still opened despite manifest exception
    assert len(module._incidents) == 1
    inc = next(iter(module._incidents.values()))
    assert inc.topology["services"][0]["tier"] == "standard"


async def test_fallback_skipped_when_cursor_ahead_of_cutoff(fake_client, config):
    """R5 edge case fix: if breach_after >= cutoff (clock skew or stalled
    worker), the worker must not request created_after > created_before
    from the API (undefined at the core API level)."""
    fake_client.get_assessments = AsyncMock(return_value=_ok_result([]))
    fake_client.get_verdicts = AsyncMock(return_value=_ok_result([]))

    module = RespondModule(client=fake_client, config=config)
    # Cursor far in the future relative to (now - fallback_threshold_seconds)
    future = (datetime.now(UTC) + timedelta(days=365)).isoformat()
    module._cursors.breach_after = future

    await module._ingest_triggers()
    # The verdict-poll must have been skipped — get_verdicts not called
    fake_client.get_verdicts.assert_not_awaited()


async def test_breach_path_cursor_advances_on_open_failure(fake_client, config):
    """R5 correctness fix: a malformed breach must not stall the cursor.

    The wrapper try/except in _ingest_orphan_breaches catches open failures,
    logs them, and still advances the cursor so the next cycle moves past
    the bad record instead of looping forever.
    """
    bad_breach = {
        "id": "vrd-bad",
        "created_at": "2026-04-25T10:00:00+00:00",
        # Missing service field — open_from_breach falls back to "unknown"
    }
    fake_client.get_assessments = AsyncMock(return_value=_ok_result([]))
    fake_client.get_verdicts = AsyncMock(return_value=_ok_result([bad_breach]))
    fake_client.get_manifest = AsyncMock(return_value=_err_result(404))

    module = RespondModule(client=fake_client, config=config)
    await module._ingest_triggers()
    # Cursor advanced past the bad record
    assert module._cursors.breach_after == "2026-04-25T10:00:00+00:00"


async def test_drive_no_incidents_no_coordinator_init(fake_client, config):
    """If there are no active incidents, agent/coordinator construction is skipped."""
    fake_client.get_assessments = AsyncMock(return_value=_ok_result([]))
    fake_client.get_verdicts = AsyncMock(return_value=_ok_result([]))

    module = RespondModule(client=fake_client, config=config)
    await module.process_cycle()

    assert module._coordinator is None
    assert module._agents is None


async def test_ingest_breach_cursor_advances(fake_client, config):
    breaches = [
        _breach(breach_id="vrd-1", created_at="2026-04-25T10:00:00+00:00"),
        _breach(breach_id="vrd-2", created_at="2026-04-25T11:00:00+00:00"),
    ]
    fake_client.get_assessments = AsyncMock(return_value=_ok_result([]))
    fake_client.get_verdicts = AsyncMock(return_value=_ok_result(breaches))

    module = RespondModule(client=fake_client, config=config)
    await module.process_cycle()
    assert module._cursors.breach_after == "2026-04-25T11:00:00+00:00"


# ------------------------------------------------------------------ #
# Eager case creation (opensrm-saun.1.2)                                #
# ------------------------------------------------------------------ #

async def test_snapshot_path_creates_case(fake_client, config):
    """Opening an incident from a snapshot eagerly POSTs a case to core,
    anchored on the breach verdict that triggered the snapshot."""
    snap = _snapshot(parent_ids=["vrd-breach-x"])
    fake_client.get_assessments = AsyncMock(return_value=_ok_result([snap]))
    fake_client.get_verdicts = AsyncMock(return_value=_ok_result([]))
    fake_client.get_manifest = AsyncMock(
        return_value=_ok_result({"name": "fraud-detect", "tier": "critical"})
    )
    fake_client.create_case = AsyncMock(return_value=_ok_result({"id": "case-x", "priority": "P1"}))

    module = RespondModule(client=fake_client, config=config)
    await module._ingest_triggers()

    fake_client.create_case.assert_awaited_once()
    case = fake_client.create_case.await_args.args[0]
    assert case["kind"] == "incident"
    assert case["service"] == "fraud-detect"
    # Anchor: first parent_id from snapshot data (the triggering breach).
    assert case["underlying_verdict"] == "vrd-breach-x"
    assert case["has_active_incident"] is True
    # Environment from the snapshot's domain — feeds core's _derive_priority.
    assert case["blast_radius"] == "production"
    assert case["briefing"]


async def test_breach_path_creates_case(fake_client, config):
    """Fallback path: incident opened from a quality_breach verdict creates
    a case anchored directly on the breach (no snapshot to anchor on)."""
    breach = _breach(breach_id="vrd-breach-only")
    breach["data"] = {"slo_name": "reversal_rate", "current_value": 0.08, "target": 0.015}
    # Force fallback by aging the breach past the threshold; threshold=1s in
    # the test config and breach is from 2026-04-25.
    fake_client.get_assessments = AsyncMock(return_value=_ok_result([]))
    fake_client.get_verdicts = AsyncMock(return_value=_ok_result([breach]))
    fake_client.get_manifest = AsyncMock(
        return_value=_ok_result({"name": "fraud-detect", "tier": "critical"})
    )
    fake_client.create_case = AsyncMock(return_value=_ok_result({"id": "case-y", "priority": "P1"}))

    module = RespondModule(client=fake_client, config=config)
    await module._ingest_triggers()

    fake_client.create_case.assert_awaited_once()
    case = fake_client.create_case.await_args.args[0]
    assert case["underlying_verdict"] == "vrd-breach-only"
    assert case["service"] == "fraud-detect"
    assert "reversal_rate" in case["briefing"]
    assert "0.08" in case["briefing"]


async def test_case_create_failure_does_not_crash_cycle(fake_client, config):
    """If core rejects POST /cases, the cycle continues and the incident
    proceeds through the agent pipeline. Bench may not see this incident
    but the verdict chain is still correct."""
    snap = _snapshot(parent_ids=["vrd-breach-x"])
    fake_client.get_assessments = AsyncMock(return_value=_ok_result([snap]))
    fake_client.get_verdicts = AsyncMock(return_value=_ok_result([]))
    fake_client.get_manifest = AsyncMock(
        return_value=_ok_result({"name": "fraud-detect", "tier": "critical"})
    )
    fake_client.create_case = AsyncMock(return_value=_err_result(status=503))

    module = RespondModule(client=fake_client, config=config)
    # Must not raise.
    await module._ingest_triggers()

    # Incident is still opened despite case-create failure.
    assert len(module._incidents) == 1


async def test_snapshot_with_no_parent_ids_skips_case_creation(fake_client, config):
    """If a snapshot has no triggering breach verdict (cold start, post-restart,
    or future snapshot kind without QUALITY_SCORE events), the incident still
    opens but no case is created. case.underlying_verdict must anchor on a
    verdict id so bench's lineage walk resolves; using the snapshot's
    assessment id would 404 on GET /verdicts/{id}/ancestors."""
    snap = _snapshot(parent_ids=[])  # No triggering breach
    fake_client.get_assessments = AsyncMock(return_value=_ok_result([snap]))
    fake_client.get_verdicts = AsyncMock(return_value=_ok_result([]))
    fake_client.get_manifest = AsyncMock(
        return_value=_ok_result({"name": "fraud-detect", "tier": "critical"})
    )
    fake_client.create_case = AsyncMock(return_value=_ok_result({"id": "x"}))

    module = RespondModule(client=fake_client, config=config)
    await module._ingest_triggers()

    # Incident is opened …
    assert len(module._incidents) == 1
    # … but no case was created (no verdict to anchor on).
    fake_client.create_case.assert_not_awaited()


async def test_case_id_derived_from_incident_id(fake_client, config):
    """Case id is deterministically derived from incident id so a replay
    can dedup via 409 rather than creating duplicates."""
    snap = _snapshot(parent_ids=["vrd-breach-x"])
    fake_client.get_assessments = AsyncMock(return_value=_ok_result([snap]))
    fake_client.get_verdicts = AsyncMock(return_value=_ok_result([]))
    fake_client.get_manifest = AsyncMock(
        return_value=_ok_result({"name": "fraud-detect", "tier": "critical"})
    )
    fake_client.create_case = AsyncMock(return_value=_ok_result({"id": "case-x"}))

    module = RespondModule(client=fake_client, config=config)
    await module._ingest_triggers()

    inc_id = next(iter(module._incidents))
    case = fake_client.create_case.await_args.args[0]
    assert case["id"] == f"case-{inc_id}"
