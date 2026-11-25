"""An incident's retrospective is built from its verdict chain, found by
lineage, and written once the incident has run its course [opensrm-91pa].

The chain is read through ``tests/learn/core_fake.py``, whose lineage rule
is a copy of core's and not a check of it: see that module's docstring.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest
import structlog

from nthlayer_workers.learn.worker import (
    CLOSED_BY_MAX_AGE,
    CLOSED_BY_QUIET,
    IncidentChain,
    LearnRetrospectiveModule,
    fetch_incident_chain,
    has_open_case,
    retrospective_closure,
)
from nthlayer_workers.respond.config import RespondConfig
from nthlayer_workers.respond.worker_helpers import open_from_snapshot
from tests.learn.core_fake import FakeCore

NOW = datetime.now(UTC)


def _at(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def _snapshot(snapshot_id: str = "csn-fraud-1", *, minutes_ago: float = 30,
              triggers: tuple[str, ...] = ("vrd-breach-1",), service: str = "fraud-detect") -> dict:
    """A correlation_snapshot as correlate emits it (correlate/worker.py
    ``_emit_snapshot``): the trigger verdict ids are ``data.parent_ids``."""
    return {
        "id": snapshot_id,
        "created_at": _at(minutes_ago),
        "kind": "correlation_snapshot",
        "service": service,
        "data": {
            "domain": {"service": service, "environment": "production"},
            "window": {
                "opened_at": _at(minutes_ago + 5), "closed_at": _at(minutes_ago),
                "duration_seconds": 300, "close_reason": "gap",
            },
            "event_count": 2,
            "affected_services": [service],
            "correlation_groups": [],
            "parent_ids": list(triggers),
        },
    }


def _breach(core: FakeCore, vid: str = "vrd-breach-1", *, minutes_ago: float = 33,
            service: str = "fraud-detect") -> dict:
    """A quality_breach verdict from measure: no parents, no outcome key."""
    return core.put_verdict({
        "id": vid, "type": "quality_breach", "service": service,
        "created_at": _at(minutes_ago), "parent_ids": [],
    })


def _respond_chain(core: FakeCore, snapshot: dict, *, first_minutes_ago: float = 28,
                   steps: tuple[str, ...] = ("triage", "investigation", "communication",
                                             "remediation")) -> list[dict]:
    """Respond's verdicts for the incident *snapshot* opens, linked the way
    respond links them (respond/agents/base.py): the first verdict's
    ``parent_ids`` are the incident's ``trigger_verdict_ids``, taken here
    from respond's own ``open_from_snapshot``; each later one names the
    verdict before it."""
    incident = open_from_snapshot(snapshot, RespondConfig())
    chain: list[dict] = []
    for n, step in enumerate(steps):
        parents = [chain[-1]["id"]] if chain else list(incident.trigger_verdict_ids)
        chain.append(core.put_verdict({
            "id": f"vrd-{snapshot['id']}-{step}", "type": step,
            "service": incident.service, "created_at": _at(first_minutes_ago - n * 0.25),
            "parent_ids": parents,
        }))
    return chain


def _outcome(core: FakeCore, original_id: str, *, minutes_ago: float = 1,
             status: str = "confirmed") -> dict:
    """The child core creates on POST /verdicts/{id}/outcome."""
    return core.put_verdict({
        "id": f"out-{original_id}", "type": "outcome_resolution",
        "created_at": _at(minutes_ago), "parent_ids": [original_id],
        "outcome": {"status": status},
    })


def _incident(core: FakeCore, **snapshot_kwargs) -> tuple[dict, list[dict]]:
    """A snapshot in core with its trigger and respond's four verdicts."""
    snapshot = _snapshot(**snapshot_kwargs)
    for trigger in snapshot["data"]["parent_ids"]:
        if trigger not in core.verdicts:
            _breach(core, trigger, minutes_ago=snapshot_kwargs.get("minutes_ago", 30) + 3,
                    service=snapshot["service"])
    core.assessments.append(snapshot)
    return snapshot, _respond_chain(
        core, snapshot, first_minutes_ago=snapshot_kwargs.get("minutes_ago", 30) - 2
    )


# ------------------------------------------------------------------ #
# fetch_incident_chain                                                 #
# ------------------------------------------------------------------ #


async def test_the_chain_is_the_trigger_and_everything_respond_did():
    core = FakeCore()
    snapshot, respond = _incident(core)

    chain = await fetch_incident_chain(core, snapshot)

    assert [v["id"] for v in chain.decisions] == [
        "vrd-breach-1",
        "vrd-csn-fraud-1-triage",
        "vrd-csn-fraud-1-investigation",
        "vrd-csn-fraud-1-communication",
        "vrd-csn-fraud-1-remediation",
    ]
    assert chain.resolved_ids == frozenset()
    # Respond acted after the snapshot's window closed: a query bounded by
    # the window, as the chain used to be built, returned none of these.
    closed_at = snapshot["data"]["window"]["closed_at"]
    assert all(v["created_at"] > closed_at for v in respond)


async def test_another_incident_on_the_same_service_is_not_in_the_chain():
    core = FakeCore()
    snapshot, _ = _incident(core)
    other, other_respond = _incident(
        core, snapshot_id="csn-fraud-2", triggers=("vrd-breach-2",), minutes_ago=29
    )
    core.put_verdict({"id": "vrd-stray", "type": "approval", "service": "fraud-detect",
                      "created_at": _at(27), "parent_ids": []})

    chain = await fetch_incident_chain(core, snapshot)

    assert chain.ids.isdisjoint({v["id"] for v in other_respond} | {"vrd-breach-2", "vrd-stray"})
    assert len(chain.decisions) == 5


async def test_two_snapshots_sharing_a_trigger_keep_their_own_respond_chains():
    """Both respond chains descend from the shared trigger; only the
    snapshot id tells them apart."""
    core = FakeCore()
    first, first_respond = _incident(core, snapshot_id="csn-fraud-1")
    second, second_respond = _incident(core, snapshot_id="csn-fraud-2", minutes_ago=20)

    chain = await fetch_incident_chain(core, first)

    assert chain.ids == {"vrd-breach-1"} | {v["id"] for v in first_respond}
    assert chain.ids.isdisjoint({v["id"] for v in second_respond})


async def test_outcome_resolutions_are_not_decisions_but_mark_theirs_resolved():
    core = FakeCore()
    snapshot, respond = _incident(core)
    _outcome(core, "vrd-breach-1")
    _outcome(core, respond[0]["id"], status="overridden")

    chain = await fetch_incident_chain(core, snapshot)

    assert len(chain.decisions) == 5
    assert all(v["type"] != "outcome_resolution" for v in chain.decisions)
    # Whether a child exists, never which outcome it carries.
    assert chain.resolved_ids == {"vrd-breach-1", respond[0]["id"]}


async def test_an_outcome_of_another_incidents_decision_resolves_nothing_here():
    core = FakeCore()
    first, _ = _incident(core, snapshot_id="csn-fraud-1")
    _second, second_respond = _incident(core, snapshot_id="csn-fraud-2", minutes_ago=20)
    _outcome(core, second_respond[0]["id"])

    chain = await fetch_incident_chain(core, first)

    assert chain.resolved_ids == frozenset()


async def test_a_snapshot_respond_never_acted_on_is_just_its_triggers():
    core = FakeCore()
    snapshot = _snapshot(triggers=("vrd-breach-1", "vrd-breach-2"))
    _breach(core, "vrd-breach-1", minutes_ago=34)
    _breach(core, "vrd-breach-2", minutes_ago=33)

    chain = await fetch_incident_chain(core, snapshot)

    assert [v["id"] for v in chain.decisions] == ["vrd-breach-1", "vrd-breach-2"]


async def test_a_snapshot_with_no_trigger_verdicts_still_finds_respond():
    """A window with no breach verdict: respond's first verdict names the
    snapshot id alone."""
    core = FakeCore()
    snapshot = _snapshot(triggers=())
    respond = _respond_chain(core, snapshot)
    assert respond[0]["parent_ids"] == ["csn-fraud-1"]

    chain = await fetch_incident_chain(core, snapshot)

    assert chain.ids == {v["id"] for v in respond}


async def test_an_empty_chain_has_no_newest_timestamp():
    chain = await fetch_incident_chain(FakeCore(), _snapshot(triggers=()))

    assert chain == IncidentChain(decisions=(), resolved_ids=frozenset())
    assert chain.newest_created_at is None


async def test_a_trigger_core_no_longer_holds_is_skipped():
    core = FakeCore()
    snapshot = _snapshot(triggers=("vrd-pruned", "vrd-breach-1"))
    _breach(core, "vrd-breach-1")
    respond = _respond_chain(core, snapshot)

    chain = await fetch_incident_chain(core, snapshot)

    assert chain.ids == {"vrd-breach-1"} | {v["id"] for v in respond}


@pytest.mark.parametrize("parent_ids", [None, "vrd-breach-1", [None, 7, "", "vrd-breach-1"]])
async def test_malformed_trigger_ids_are_ignored(parent_ids):
    core = FakeCore()
    snapshot = _snapshot()
    snapshot["data"]["parent_ids"] = parent_ids
    _breach(core)

    chain = await fetch_incident_chain(core, snapshot)

    expected = {"vrd-breach-1"} if isinstance(parent_ids, list) else set()
    assert chain.ids == expected


@pytest.mark.parametrize("method", ["get_descendants", "get_verdict"])
async def test_a_failed_read_is_no_chain_at_all(method):
    """Half a chain would be written up as the whole incident."""
    core = FakeCore()
    snapshot, _ = _incident(core)
    core.fail.add(method)

    assert await fetch_incident_chain(core, snapshot) is None


async def test_newest_created_at_is_the_last_decision_not_an_outcome():
    core = FakeCore()
    snapshot, respond = _incident(core)
    _outcome(core, respond[-1]["id"], minutes_ago=0.5)

    chain = await fetch_incident_chain(core, snapshot)

    assert chain.newest_created_at == datetime.fromisoformat(respond[-1]["created_at"])


# ------------------------------------------------------------------ #
# has_open_case                                                        #
# ------------------------------------------------------------------ #

_ANCHORS = frozenset({"csn-fraud-1", "vrd-breach-1", "vrd-x"})


@pytest.mark.parametrize(
    ("state", "underlying", "expected"),
    [
        ("pending", "vrd-breach-1", True),
        ("leased", "vrd-breach-1", True),
        ("resolved", "vrd-breach-1", False),
        ("pending", "vrd-someone-elses", False),
        # A case anchored on the snapshot id itself.
        ("pending", "csn-fraud-1", True),
    ],
)
async def test_an_unresolved_case_on_the_chain_is_an_open_case(state, underlying, expected):
    core = FakeCore()
    core.put_case("case-INC-1", underlying, state)

    assert await has_open_case(core, _ANCHORS) is expected


async def test_no_cases_is_no_open_case():
    assert await has_open_case(FakeCore(), _ANCHORS) is False


async def test_a_failed_case_read_cannot_rule_one_out():
    core = FakeCore()
    core.fail.add("get_cases")

    assert await has_open_case(core, _ANCHORS) is None


async def test_a_full_page_of_open_cases_cannot_rule_one_out():
    core = FakeCore()
    for n in range(100):
        core.put_case(f"case-{n}", f"vrd-other-{n}", "pending")

    assert await has_open_case(core, _ANCHORS) is None


async def test_a_full_page_that_holds_the_anchoring_case_is_an_open_case():
    core = FakeCore()
    for n in range(99):
        core.put_case(f"case-{n}", f"vrd-other-{n}", "pending")
    core.put_case("case-mine", "vrd-breach-1", "pending")

    assert await has_open_case(core, _ANCHORS) is True


# ------------------------------------------------------------------ #
# retrospective_closure                                                #
# ------------------------------------------------------------------ #

_QUIET = timedelta(minutes=10)
_MAX_AGE = timedelta(hours=24)


def _chain_ending(minutes_ago: float | None) -> IncidentChain:
    decisions = () if minutes_ago is None else (
        {"id": "v", "created_at": (NOW - timedelta(minutes=minutes_ago)).isoformat()},
    )
    return IncidentChain(decisions=decisions, resolved_ids=frozenset())


def _closure(*, snapshot_minutes_ago, newest_minutes_ago, open_case):
    return retrospective_closure(
        now=NOW,
        snapshot_created_at=NOW - timedelta(minutes=snapshot_minutes_ago),
        chain=_chain_ending(newest_minutes_ago),
        open_case=open_case,
        quiet=_QUIET,
        max_age=_MAX_AGE,
    )


@pytest.mark.parametrize(
    ("snapshot_minutes_ago", "newest_minutes_ago", "open_case", "expected"),
    [
        # Quiet for the whole period, no open case.
        (30, 10, False, CLOSED_BY_QUIET),
        (30, 25, False, CLOSED_BY_QUIET),
        # A verdict joined inside the quiet period.
        (30, 9.9, False, None),
        (30, 0, False, None),
        # An open case, or one that cannot be ruled out, waits.
        (30, 25, True, None),
        (30, 25, None, None),
        # Respond has not acted yet: the clock runs from the snapshot, not
        # from a trigger that predates it.
        (5, 40, False, None),
        (10, 40, False, CLOSED_BY_QUIET),
        # Nothing in the chain at all: the snapshot is the last activity.
        (5, None, False, None),
        (10, None, False, CLOSED_BY_QUIET),
        # The maximum age closes whatever the incident's state.
        (24 * 60, 0, True, CLOSED_BY_MAX_AGE),
        (24 * 60, 0, None, CLOSED_BY_MAX_AGE),
        (24 * 60 + 5, 500, False, CLOSED_BY_MAX_AGE),
        (24 * 60 - 1, 0, True, None),
    ],
)
def test_when_a_retrospective_closes(snapshot_minutes_ago, newest_minutes_ago, open_case, expected):
    assert _closure(
        snapshot_minutes_ago=snapshot_minutes_ago,
        newest_minutes_ago=newest_minutes_ago,
        open_case=open_case,
    ) == expected


def test_a_snapshot_with_no_readable_age_is_closed_not_held_forever():
    assert retrospective_closure(
        now=NOW, snapshot_created_at=None, chain=_chain_ending(0), open_case=True,
        quiet=_QUIET, max_age=_MAX_AGE,
    ) == CLOSED_BY_MAX_AGE


# ------------------------------------------------------------------ #
# The module                                                           #
# ------------------------------------------------------------------ #


async def test_a_quiet_incident_gets_one_retrospective_with_responds_verdicts():
    core = FakeCore()
    snapshot, respond = _incident(core, minutes_ago=30)
    _outcome(core, respond[0]["id"])
    module = LearnRetrospectiveModule(client=core)

    await module.process_cycle()
    await module.process_cycle()

    (retro,) = core.retrospectives
    data = retro["data"]
    assert retro["service"] == "fraud-detect"
    assert data["correlation_snapshot_id"] == "csn-fraud-1"
    assert data["closed_by"] == "quiet"
    assert data["verdict_count"] == 5
    assert [entry["type"] for entry in data["timeline"]] == [
        "quality_breach", "triage", "investigation", "communication", "remediation",
    ]
    assert data["outcome_coverage"] == {"resolved": 1, "pending": 4, "total": 5}
    assert data["decisions_affected"] == 1
    assert await module.get_state() == {"cursor": snapshot["created_at"]}


async def test_an_incident_still_in_motion_is_held_and_survives_a_restart():
    core = FakeCore()
    snapshot, _ = _incident(core, minutes_ago=4)
    module = LearnRetrospectiveModule(client=core)

    await module.process_cycle()

    assert core.retrospectives == []
    state = await module.get_state()
    assert state == {"cursor": snapshot["created_at"], "pending": {"csn-fraud-1": snapshot}}

    # A new process: the snapshot is behind the cursor, and is still held.
    restarted = LearnRetrospectiveModule(client=core)
    await restarted.restore_state(state)
    await restarted.process_cycle()

    assert core.retrospectives == []
    assert (await restarted.get_state())["pending"] == {"csn-fraud-1": snapshot}


async def test_a_restart_does_not_reset_the_quiet_clock():
    """The state is restored into a new process that has never seen the
    snapshot; it closes at once, because core's timestamps say it is quiet."""
    core = FakeCore()
    snapshot, _ = _incident(core, minutes_ago=30)
    restarted = LearnRetrospectiveModule(client=core)
    await restarted.restore_state(
        {"cursor": snapshot["created_at"], "pending": {"csn-fraud-1": snapshot}}
    )

    await restarted.process_cycle()

    (retro,) = core.retrospectives
    assert retro["data"]["closed_by"] == "quiet"
    assert "pending" not in await restarted.get_state()


async def test_an_open_case_holds_a_quiet_incident_until_it_resolves():
    core = FakeCore()
    _incident(core, minutes_ago=30)
    core.put_case("case-INC-1", "vrd-breach-1", "pending")
    module = LearnRetrospectiveModule(client=core)

    await module.process_cycle()
    assert core.retrospectives == []

    core.cases["case-INC-1"]["state"] = "resolved"
    await module.process_cycle()

    (retro,) = core.retrospectives
    assert retro["data"]["closed_by"] == "quiet"


async def test_the_maximum_age_writes_up_an_incident_that_is_still_open():
    core = FakeCore()
    _incident(core, minutes_ago=24 * 60 + 1)
    core.put_case("case-INC-1", "vrd-breach-1", "leased")
    module = LearnRetrospectiveModule(client=core)

    await module.process_cycle()

    (retro,) = core.retrospectives
    assert retro["data"]["closed_by"] == "max_age"
    assert retro["data"]["verdict_count"] == 5


async def test_the_quiet_period_and_maximum_age_are_the_modules_own():
    core = FakeCore()
    _incident(core, minutes_ago=4)
    core.put_case("case-INC-1", "vrd-breach-1", "pending")

    await LearnRetrospectiveModule(client=core, quiet_minutes=1).process_cycle()
    assert core.retrospectives == []  # quiet, but the case is open

    await LearnRetrospectiveModule(client=core, max_age_hours=0.05).process_cycle()
    assert [r["data"]["closed_by"] for r in core.retrospectives] == ["max_age"]

    core.cases.clear()
    await LearnRetrospectiveModule(client=core, quiet_minutes=1).process_cycle()
    assert [r["data"]["closed_by"] for r in core.retrospectives] == ["max_age", "quiet"]


async def test_a_failed_submission_keeps_the_snapshot_pending():
    core = FakeCore()
    snapshot, _ = _incident(core, minutes_ago=30)
    module = LearnRetrospectiveModule(client=core)
    core.fail.add("submit_assessment")

    await module.process_cycle()

    assert core.retrospectives == []
    assert (await module.get_state())["pending"] == {"csn-fraud-1": snapshot}

    core.fail.clear()
    await module.process_cycle()

    assert len(core.retrospectives) == 1
    assert "pending" not in await module.get_state()


async def test_an_unreadable_chain_keeps_the_snapshot_pending_and_says_so():
    core = FakeCore()
    snapshot, _ = _incident(core, minutes_ago=24 * 60 + 1)
    module = LearnRetrospectiveModule(client=core)
    core.fail.add("get_descendants")

    with structlog.testing.capture_logs() as logs:
        await module.process_cycle()

    # Past the maximum age, and still not written from half a chain.
    assert core.retrospectives == []
    assert (await module.get_state())["pending"] == {"csn-fraud-1": snapshot}
    (event,) = [e for e in logs if e["event"] == "retrospective_chain_unreadable"]
    assert (event["snapshot_id"], event["log_level"]) == ("csn-fraud-1", "warning")


async def test_pending_snapshots_are_still_worked_when_the_snapshot_read_fails():
    core = FakeCore()
    snapshot, _ = _incident(core, minutes_ago=30)
    module = LearnRetrospectiveModule(client=core)
    await module.restore_state(
        {"cursor": snapshot["created_at"], "pending": {"csn-fraud-1": snapshot}}
    )
    core.fail.add("get_assessments")

    await module.process_cycle()

    assert len(core.retrospectives) == 1


async def test_a_snapshot_behind_the_cursor_is_not_taken_again():
    core = FakeCore()
    snapshot, _ = _incident(core, minutes_ago=30)
    module = LearnRetrospectiveModule(client=core)
    await module.restore_state({"cursor": snapshot["created_at"]})

    await module.process_cycle()

    assert core.retrospectives == []
    assert "pending" not in await module.get_state()


async def test_each_pending_snapshot_closes_on_its_own_chain():
    core = FakeCore()
    _incident(core, snapshot_id="csn-fraud-1", minutes_ago=30)
    _incident(core, snapshot_id="csn-fraud-2", triggers=("vrd-breach-2",), minutes_ago=4)
    module = LearnRetrospectiveModule(client=core)

    await module.process_cycle()

    assert [r["data"]["correlation_snapshot_id"] for r in core.retrospectives] == ["csn-fraud-1"]
    state = await module.get_state()
    assert list(state["pending"]) == ["csn-fraud-2"]
    assert state["cursor"] == core.assessments[1]["created_at"]


@pytest.mark.parametrize("pending", [None, [], "csn-1", {"csn-1": "not a snapshot", 7: {}}])
async def test_a_malformed_pending_state_is_dropped_not_raised(pending):
    module = LearnRetrospectiveModule(client=FakeCore())

    await module.restore_state({"cursor": "2026-01-01T00:00:00+00:00", "pending": pending})

    assert await module.get_state() == {"cursor": "2026-01-01T00:00:00+00:00"}


def test_an_incident_chain_is_frozen():
    chain = IncidentChain(decisions=(), resolved_ids=frozenset())

    with pytest.raises(dataclasses.FrozenInstanceError):
        chain.decisions = ({"id": "v"},)
