"""Respond resolves the case it opened, once the incident has resolved
[opensrm-j9io].

Core's side is ``PUT /cases/{id}/resolve`` (nthlayer-core server.py
``put_case_resolve``): a non-empty ``resolution_id`` in the body, 200 on
success, 404 for a case it does not hold, 409 for one already resolved.
``_Cases`` below copies those answers; it does not check them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
import structlog
from nthlayer_common.api_client import APIResult

from nthlayer_workers.respond.config import RespondConfig
from nthlayer_workers.respond.context_store import (
    incident_context_from_dict,
    incident_context_to_dict,
)
from nthlayer_workers.respond.types import IncidentContext, IncidentState
from nthlayer_workers.respond.worker import CASE_RESOLUTION, RespondModule


def _ok(data=None, status_code: int = 200) -> APIResult:
    return APIResult(ok=True, status_code=status_code, data=data, error=None, detail=None)


def _refused(status_code: int, error: str) -> APIResult:
    return APIResult(ok=False, status_code=status_code, data=None, error=error, detail=None)


class _Cases:
    """Core's cases, behind the two client methods respond uses."""

    def __init__(self) -> None:
        self.cases: dict[str, dict] = {}
        self.unreachable = False
        self.unreachable_status = 0
        self.resolve_calls: list[tuple[str, str]] = []

    async def create_case(self, case: dict) -> APIResult:
        self.cases[case["id"]] = {**case, "state": "pending", "resolution_id": None}
        return _ok({"id": case["id"]}, status_code=201)

    async def resolve_case(self, case_id: str, resolution_id: str) -> APIResult:
        self.resolve_calls.append((case_id, resolution_id))
        if self.unreachable:
            # The client's answer when it could not reach core at all:
            # status 0 (CoreAPIClient._request, after its retries).
            return _refused(self.unreachable_status, "connection_failed")
        if not resolution_id:
            return _refused(422, "missing_fields")
        case = self.cases.get(case_id)
        if case is None:
            return _refused(404, "not_found")
        if case["state"] == "resolved":
            return _refused(409, "already_resolved")
        case.update(state="resolved", resolution_id=resolution_id)
        return _ok({"resolved": True, "resolution_id": resolution_id})


def _module(cases: _Cases) -> RespondModule:
    client = AsyncMock()
    client.get_assessments = AsyncMock(return_value=_ok([]))
    client.get_verdicts = AsyncMock(return_value=_ok([]))
    client.create_case = cases.create_case
    client.resolve_case = cases.resolve_case
    return RespondModule(
        client=client,
        config=RespondConfig(cycle_interval_seconds=1.0, terminal_retention_seconds=3600.0),
    )


def _incident(state: IncidentState, *, incident_id: str = "INC-FRAUD-1",
              verdict_chain: tuple[str, ...] = ("vrd-triage", "vrd-remediation")) -> IncidentContext:
    now = datetime.now(UTC).isoformat()
    return IncidentContext(
        id=incident_id, state=state, created_at=now, updated_at=now,
        trigger_source="nthlayer-correlate",
        trigger_verdict_ids=["csn-fraud-1", "vrd-breach-1"],
        topology={}, verdict_chain=list(verdict_chain), service="fraud-detect",
    )


async def _open_case(module: RespondModule, context: IncidentContext) -> None:
    """Open the incident's case the way respond does."""
    await module._create_case_for_incident(
        context, underlying_verdict="vrd-breach-1", service="fraud-detect",
        environment="production", briefing="b",
    )


async def test_a_resolved_incidents_case_is_resolved_with_its_last_verdict():
    cases = _Cases()
    module = _module(cases)
    context = _incident(IncidentState.RESOLVED)
    module._incidents[context.id] = context
    await _open_case(module, context)

    await module.process_cycle()

    case = cases.cases["case-INC-FRAUD-1"]
    assert (case["state"], case["resolution_id"]) == ("resolved", "vrd-remediation")
    assert context.metadata[CASE_RESOLUTION] == "resolved"


# Every state but RESOLVED, from the enum, so a state added later is covered.
# Guarded: an empty list would collect no test at all, and the three states
# the ruling names must be among them.
_OTHER_STATES = [s for s in IncidentState if s is not IncidentState.RESOLVED]
assert _OTHER_STATES
assert {
    IncidentState.ESCALATED, IncidentState.FAILED, IncidentState.AWAITING_APPROVAL,
} <= set(_OTHER_STATES), _OTHER_STATES


@pytest.mark.parametrize("state", _OTHER_STATES)
async def test_any_other_state_leaves_the_case_open(state):
    """ESCALATED, FAILED and AWAITING_APPROVAL need a person; the rest are
    still in motion."""
    cases = _Cases()
    module = _module(cases)
    context = _incident(state)
    module._incidents[context.id] = context
    await _open_case(module, context)
    # Keep the incident in the state under test for this cycle.
    module._coordinator = AsyncMock()
    module._coordinator.run = AsyncMock(side_effect=lambda c: c)

    await module.process_cycle()

    assert cases.resolve_calls == []
    assert cases.cases["case-INC-FRAUD-1"]["state"] == "pending"
    assert CASE_RESOLUTION not in context.metadata


async def test_the_case_is_resolved_in_the_cycle_the_incident_resolves():
    cases = _Cases()
    module = _module(cases)
    context = _incident(IncidentState.REMEDIATING, verdict_chain=("vrd-triage",))
    module._incidents[context.id] = context
    await _open_case(module, context)

    async def finish(c):
        c.verdict_chain.append("vrd-final")
        c.state = IncidentState.RESOLVED
        return c

    module._coordinator = AsyncMock()
    module._coordinator.run = AsyncMock(side_effect=finish)

    await module.process_cycle()

    assert cases.cases["case-INC-FRAUD-1"]["resolution_id"] == "vrd-final"


async def test_a_case_is_resolved_once():
    cases = _Cases()
    module = _module(cases)
    context = _incident(IncidentState.RESOLVED)
    module._incidents[context.id] = context
    await _open_case(module, context)

    for _cycle in range(3):
        await module.process_cycle()

    assert cases.resolve_calls == [("case-INC-FRAUD-1", "vrd-remediation")]


async def test_a_case_someone_else_resolved_is_left_as_they_resolved_it():
    cases = _Cases()
    module = _module(cases)
    context = _incident(IncidentState.RESOLVED)
    module._incidents[context.id] = context
    await _open_case(module, context)
    await cases.resolve_case("case-INC-FRAUD-1", "vrd-operator")
    cases.resolve_calls.clear()

    for _cycle in range(2):
        await module.process_cycle()

    assert cases.cases["case-INC-FRAUD-1"]["resolution_id"] == "vrd-operator"
    assert context.metadata[CASE_RESOLUTION] == "already_resolved"
    assert len(cases.resolve_calls) == 1


async def test_an_incident_with_no_case_is_settled_without_one():
    """No case is opened for a snapshot with no breach verdict."""
    cases = _Cases()
    module = _module(cases)
    context = _incident(IncidentState.RESOLVED)
    module._incidents[context.id] = context

    for _cycle in range(2):
        await module.process_cycle()

    assert context.metadata[CASE_RESOLUTION] == "no_case"
    assert len(cases.resolve_calls) == 1


@pytest.mark.parametrize("status", [0, 500, 503, 401, 422])
async def test_an_answer_that_settles_nothing_is_tried_again_next_cycle(status):
    cases = _Cases()
    cases.unreachable_status = status
    module = _module(cases)
    context = _incident(IncidentState.RESOLVED)
    module._incidents[context.id] = context
    await _open_case(module, context)
    cases.unreachable = True

    with structlog.testing.capture_logs() as logs:
        await module.process_cycle()

    assert CASE_RESOLUTION not in context.metadata
    assert cases.cases["case-INC-FRAUD-1"]["state"] == "pending"
    (event,) = [e for e in logs if e["event"] == "respond_case_resolve_failed"]
    assert (event["incident_id"], event["status_code"], event["log_level"]) == (
        "INC-FRAUD-1", status, "warning",
    )

    cases.unreachable = False
    await module.process_cycle()

    assert cases.cases["case-INC-FRAUD-1"]["state"] == "resolved"
    assert context.metadata[CASE_RESOLUTION] == "resolved"


async def test_a_core_that_is_down_costs_one_call_a_cycle_not_one_per_incident():
    cases = _Cases()
    module = _module(cases)
    for n in range(4):
        context = _incident(IncidentState.RESOLVED, incident_id=f"INC-{n}",
                            verdict_chain=(f"vrd-{n}",))
        module._incidents[context.id] = context
        await _open_case(module, context)
    cases.unreachable = True

    with structlog.testing.capture_logs() as logs:
        await module.process_cycle()

    assert len(cases.resolve_calls) == 1
    assert len([e for e in logs if e["event"] == "respond_case_resolve_failed"]) == 1

    cases.unreachable = False
    cases.resolve_calls.clear()
    await module.process_cycle()

    assert {c["state"] for c in cases.cases.values()} == {"resolved"}
    assert len(cases.resolve_calls) == 4


async def test_an_incident_with_no_verdict_names_no_resolution():
    cases = _Cases()
    module = _module(cases)
    context = _incident(IncidentState.RESOLVED, verdict_chain=())
    module._incidents[context.id] = context
    await _open_case(module, context)

    with structlog.testing.capture_logs() as logs:
        for _cycle in range(2):
            await module.process_cycle()

    assert cases.resolve_calls == []
    assert cases.cases["case-INC-FRAUD-1"]["state"] == "pending"
    assert context.metadata[CASE_RESOLUTION] == "no_verdict"
    assert len([e for e in logs if e["event"] == "respond_case_resolve_skipped_no_verdict"]) == 1


async def test_each_resolved_incident_resolves_its_own_case():
    cases = _Cases()
    module = _module(cases)
    first = _incident(IncidentState.RESOLVED, incident_id="INC-A", verdict_chain=("vrd-a",))
    second = _incident(IncidentState.RESOLVED, incident_id="INC-B", verdict_chain=("vrd-b",))
    third = _incident(IncidentState.ESCALATED, incident_id="INC-C", verdict_chain=("vrd-c",))
    for context in (first, second, third):
        module._incidents[context.id] = context
        await _open_case(module, context)

    await module.process_cycle()

    assert {cid: c["resolution_id"] for cid, c in cases.cases.items()} == {
        "case-INC-A": "vrd-a", "case-INC-B": "vrd-b", "case-INC-C": None,
    }


async def test_how_the_case_was_settled_survives_the_saved_state():
    """Restored, the incident is not resolved a second time."""
    cases = _Cases()
    module = _module(cases)
    context = _incident(IncidentState.RESOLVED)
    module._incidents[context.id] = context
    await _open_case(module, context)
    await module.process_cycle()

    restored = incident_context_from_dict(incident_context_to_dict(context))
    again = _module(cases)
    again._incidents[restored.id] = restored
    await again.process_cycle()

    assert restored.metadata[CASE_RESOLUTION] == "resolved"
    assert cases.resolve_calls == [("case-INC-FRAUD-1", "vrd-remediation")]
