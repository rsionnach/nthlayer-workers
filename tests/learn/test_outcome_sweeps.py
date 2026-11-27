"""learn.outcome's two sweeps: resolve at the minimum age, expire at the expiry age [opensrm-lpbi].

Fixture provenance. The verdicts are measure's own ``quality_breach``, taken
from a real ``MeasureModule`` cycle (``_measure_verdicts``) and re-stamped with
an id and a creation time. ``_Core`` adds to ``_FakeCore`` the three behaviours
of nthlayer-core these sweeps depend on, each read from core's source rather
than from learn:

- ``GET /verdicts`` (``store.py`` ``query_verdicts``): ``created_at >=
  created_after`` and ``created_at <= created_before``, both inclusive,
  ``ORDER BY created_at DESC LIMIT ?``: newest first, so a page that hits the
  limit has dropped the oldest. The comparison is on text: the column holds
  ``created_at`` as the producer sent it and each bound is normalised by
  ``_normalise_timestamp``. Every stamp here is the ``+00:00`` ISO form that
  measure writes, for which text order is time order. ``server.py`` caps ``limit`` at ``LIMIT_CAP``
  (1000).
- ``POST /verdicts/{id}/outcome`` (``server.py`` ``post_verdict_outcome``):
  creates ``out-<id>`` and never mutates the original; a second POST for the
  same verdict is answered 409 ``duplicate``.
- ``POST /verdicts/{id}/override`` (``server.py`` ``post_verdict_override``)
  sets the original's ``outcome`` in place.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
import structlog
from nthlayer_common.api_client import APIResult

from nthlayer_workers.learn.worker import LearnOutcomeModule
from tests.learn.test_outcome_absent_key import _FakeCore, _measure_verdicts, _ok

# nthlayer-core server.py: LIMIT_CAP.
_CORE_LIMIT_CAP = 1000

_T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _unavailable() -> APIResult:
    return APIResult(ok=False, status_code=503, error="unavailable")


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _core_normalised(bound: str) -> str:
    """nthlayer-core store.py ``_normalise_timestamp``, for a bound that parses."""
    parsed = datetime.fromisoformat(bound)
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).isoformat()


class _Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class _Core(_FakeCore):
    def __init__(self, verdicts: list[dict]):
        super().__init__(verdicts)
        self.down_with = _unavailable()
        self.lineage_reads: list[str] = []
        self.posts: list[tuple[str, dict]] = []
        self.queries: list[dict] = []
        self.signals: list[dict] = []
        self.lineage_down = False
        self.outcome_down = False

    async def get_verdicts(self, *, created_after=None, created_before=None, limit=100, **_) -> APIResult:
        assert limit <= _CORE_LIMIT_CAP
        self.queries.append({"created_after": created_after, "created_before": created_before})
        # Core compares text: created_at as its producer sent it against the
        # bound normalised to an ISO string, and orders by that text.
        after = None if created_after is None else _core_normalised(created_after)
        before = None if created_before is None else _core_normalised(created_before)
        rows = [
            v for v in self.verdicts.values()
            if (after is None or v["created_at"] >= after)
            and (before is None or v["created_at"] <= before)
        ]
        rows.sort(key=lambda v: v["created_at"], reverse=True)
        return _ok(rows[:limit])

    async def get_descendants(self, verdict_id: str) -> APIResult:
        if self.lineage_down:
            return self.down_with
        self.lineage_reads.append(verdict_id)
        return await super().get_descendants(verdict_id)

    async def resolve_outcome(self, verdict_id: str, body: dict) -> APIResult:
        if self.outcome_down:
            return self.down_with
        if f"out-{verdict_id}" in self.verdicts:
            return APIResult(ok=False, status_code=409, error="duplicate")
        self.posts.append((verdict_id, body))
        return await super().resolve_outcome(verdict_id, body)

    async def submit_assessment(self, envelope: dict) -> APIResult:
        self.signals.append(envelope["data"])
        return _ok(status_code=201)

    def add(self, verdict: dict) -> None:
        self.verdicts[verdict["id"]] = verdict

    def override_in_place(self, verdict_id: str) -> None:
        self.verdicts[verdict_id] = {**self.verdicts[verdict_id], "outcome": {"status": "overridden"}}

    def client(self) -> AsyncMock:  # type: ignore[override]
        client = AsyncMock()
        client.get_verdicts = AsyncMock(side_effect=self.get_verdicts)
        client.get_descendants = AsyncMock(side_effect=self.get_descendants)
        client.resolve_outcome = AsyncMock(side_effect=self.resolve_outcome)
        client.submit_assessment = AsyncMock(side_effect=self.submit_assessment)
        return client

    def posted(self, status: str) -> list[str]:
        return [vid for vid, body in self.posts if body["outcome_status"] == status]


@pytest.fixture
async def breach() -> dict:
    return (await _measure_verdicts())["quality_breach"]


def _at(template: dict, verdict_id: str, created_at: datetime) -> dict:
    return {**template, "id": verdict_id, "created_at": created_at.isoformat()}


def _module(core: _Core, clock: _Clock, **kwargs) -> LearnOutcomeModule:
    return LearnOutcomeModule(client=core.client(), clock=clock, **kwargs)


def _execution(verdict_id: str, parent_id: str, created_at: datetime) -> dict:
    return {"id": verdict_id, "type": "execution", "service": "fraud-detect",
            "created_at": created_at.isoformat(), "parent_ids": [parent_id],
            "outcome": {"status": "confirmed"}}


class TestExpiryIsReachedInSteadyState:
    async def test_a_pending_verdict_is_expired_when_it_reaches_the_expiry_age(self, breach):
        """Seen at about 1h old with nothing to resolve it, then again at 7 days."""
        core = _Core([_at(breach, "vrd-1", _T0)])
        clock = _Clock(_T0 + timedelta(hours=1, minutes=1))
        module = _module(core, clock)

        await module.process_cycle()
        assert core.lineage_reads == ["vrd-1"]
        assert core.posts == []

        clock.now = _T0 + timedelta(days=3)
        await module.process_cycle()
        assert core.lineage_reads == ["vrd-1"]

        clock.now = _T0 + timedelta(days=7, minutes=1)
        await module.process_cycle()
        assert core.posted("expired") == ["vrd-1"]
        assert core.signals == []

    async def test_a_verdict_short_of_the_expiry_age_is_not_expired(self, breach):
        core = _Core([_at(breach, "vrd-1", _T0)])
        clock = _Clock(_T0 + timedelta(hours=2))
        module = _module(core, clock)
        await module.process_cycle()

        clock.now = _T0 + timedelta(days=7) - timedelta(minutes=1)
        await module.process_cycle()

        assert core.posts == []

    async def test_a_signal_that_arrived_late_is_recorded_instead_of_an_expiry(self, breach):
        core = _Core([_at(breach, "vrd-1", _T0)])
        clock = _Clock(_T0 + timedelta(hours=2))
        module = _module(core, clock)
        await module.process_cycle()

        core.add(_execution("vrd-exec", "vrd-1", _T0 + timedelta(days=2)))
        clock.now = _T0 + timedelta(days=7, minutes=1)
        await module.process_cycle()

        assert core.posted("confirmed") == ["vrd-1"]
        assert core.posted("expired") == []
        assert [s["data"]["verdict_id"] for s in core.signals] == ["vrd-1"]

    async def test_an_expiry_threshold_of_zero_expires_nothing(self, breach):
        core = _Core([_at(breach, "vrd-1", _T0)])
        clock = _Clock(_T0 + timedelta(days=30))
        module = _module(core, clock, expiry_threshold_days=0)

        await module.process_cycle()
        await module.process_cycle()

        assert core.posts == []
        assert "expiry_cursor" not in await module.get_state()

    async def test_a_verdict_younger_than_the_threshold_is_never_expired_whatever_core_returns(self, breach):
        """The expiry sweep trusts the verdict's own age, not the window it asked for."""
        verdict = _at(breach, "vrd-1", _T0)
        core = _Core([verdict])
        client = core.client()
        client.get_verdicts = AsyncMock(return_value=_ok([verdict]))
        module = LearnOutcomeModule(client=client, clock=_Clock(_T0 + timedelta(days=2)))

        await module.process_cycle()

        assert core.posts == []


class TestAResolvedVerdictIsLeftAlone:
    async def test_expired_once_then_never_posted_again(self, breach):
        core = _Core([_at(breach, "vrd-1", _T0)])
        clock = _Clock(_T0 + timedelta(days=8))
        module = _module(core, clock)

        for _ in range(3):
            await module.process_cycle()
            clock.now += timedelta(minutes=1)

        assert core.posted("expired") == ["vrd-1"]
        client = module.client
        assert client.resolve_outcome.await_count == 1

    async def test_its_own_resolution_record_is_not_a_confirmation(self, breach):
        """Core holds out-<id>: nothing is posted and no signal is emitted."""
        core = _Core([_at(breach, "vrd-1", _T0)])
        await core.resolve_outcome("vrd-1", {"outcome_status": "partial"})
        core.posts.clear()
        module = _module(core, _Clock(_T0 + timedelta(hours=2)))

        await module.process_cycle()

        module.client.resolve_outcome.assert_not_awaited()
        assert core.signals == []

    async def test_the_resolution_record_wins_over_a_later_execution(self, breach):
        """Checked first: a resolved verdict is not re-read as confirmed by lineage."""
        core = _Core([_at(breach, "vrd-1", _T0)])
        await core.resolve_outcome("vrd-1", {"outcome_status": "expired"})
        core.add(_execution("vrd-exec", "vrd-1", _T0 + timedelta(minutes=5)))
        module = _module(core, _Clock(_T0 + timedelta(hours=2)))

        await module.process_cycle()

        module.client.resolve_outcome.assert_not_awaited()
        assert core.signals == []

    async def test_a_409_is_already_resolved_not_a_failure(self, breach):
        """Another writer resolved it between the lineage read and the POST."""
        core = _Core([_at(breach, "vrd-1", _T0), _at(breach, "vrd-2", _T0 + timedelta(minutes=1))])
        core.add(_execution("vrd-exec-1", "vrd-1", _T0 + timedelta(minutes=2)))
        core.add(_execution("vrd-exec-2", "vrd-2", _T0 + timedelta(minutes=3)))
        client = core.client()
        answers = [APIResult(ok=False, status_code=409, error="duplicate")]

        async def _resolve(verdict_id, body):
            return answers.pop() if answers else await core.resolve_outcome(verdict_id, body)

        client.resolve_outcome = AsyncMock(side_effect=_resolve)
        module = LearnOutcomeModule(client=client, clock=_Clock(_T0 + timedelta(hours=2)))

        await module.process_cycle()

        assert core.posted("confirmed") == ["vrd-2"]
        assert [s["data"]["verdict_id"] for s in core.signals] == ["vrd-2"]

    async def test_a_verdict_overridden_in_place_is_still_skipped(self, breach):
        core = _Core([_at(breach, "vrd-1", _T0)])
        core.override_in_place("vrd-1")
        module = _module(core, _Clock(_T0 + timedelta(days=8)))

        await module.process_cycle()

        assert core.lineage_reads == []
        assert core.posts == []

    async def test_a_childs_resolution_record_does_not_resolve_its_parent(self):
        """Measure's autonomy_change names its quality_breach in parent_ids, so
        the change's out- record is a descendant of the breach (core's lineage
        is transitive). The breach is still unresolved."""
        verdicts = await _measure_verdicts()
        breach = _at(verdicts["quality_breach"], verdicts["quality_breach"]["id"], _T0)
        change = _at(verdicts["autonomy_change"], verdicts["autonomy_change"]["id"], _T0)
        core = _Core([breach, change])
        await core.resolve_outcome(change["id"], {"outcome_status": "expired"})
        core.posts.clear()
        module = _module(core, _Clock(_T0 + timedelta(days=8)))

        await module.process_cycle()

        assert core.posted("expired") == [breach["id"]]


class TestOldestFirstAndTruncation:
    def _backlog(self, breach: dict, count: int, *, step: timedelta) -> list[dict]:
        return [_at(breach, f"vrd-{n:05d}", _T0 + n * step) for n in range(count)]

    @pytest.mark.parametrize("cursor", [None, (_T0 - timedelta(hours=1)).isoformat()])
    async def test_a_window_larger_than_a_page_is_worked_from_its_old_end(self, breach, cursor):
        verdicts = self._backlog(breach, 2500, step=timedelta(seconds=10))
        core = _Core(verdicts)
        module = _module(core, _Clock(_T0 + timedelta(days=1)), expiry_threshold_days=0)
        await module.restore_state({"cursor": cursor} if cursor else None)

        seen: list[str] = []
        for _ in range(40):
            before = len(core.lineage_reads)
            await module.process_cycle()
            cycle = core.lineage_reads[before:]
            # Within a cycle: oldest first, and a contiguous run of the backlog.
            assert cycle == sorted(cycle)
            fresh = [vid for vid in cycle if vid not in set(seen)]
            assert fresh == [v["id"] for v in verdicts[len(seen):len(seen) + len(fresh)]]
            seen.extend(fresh)
            # Never past a truncated page: nothing unexamined is older than the cursor.
            cursor_now = (await module.get_state()).get("cursor")
            if cursor_now and len(seen) < len(verdicts):
                assert _instant(cursor_now) <= _instant(verdicts[len(seen)]["created_at"])
            if len(seen) == len(verdicts):
                break

        assert seen == [v["id"] for v in verdicts]

    async def test_a_window_that_fits_one_page_is_processed_oldest_first(self, breach):
        verdicts = self._backlog(breach, 5, step=timedelta(minutes=1))
        core = _Core(list(reversed(verdicts)))
        module = _module(core, _Clock(_T0 + timedelta(hours=3)))

        await module.process_cycle()

        assert core.lineage_reads == [v["id"] for v in verdicts]
        assert len(core.queries) == 2  # one per sweep: nothing was narrowed

    async def test_a_page_with_no_candidates_still_advances_the_cursor(self, breach):
        core = _Core([_at(breach, "vrd-1", _T0)])
        core.override_in_place("vrd-1")
        clock = _Clock(_T0 + timedelta(hours=3))
        module = _module(core, clock)

        await module.process_cycle()

        assert _instant((await module.get_state())["cursor"]) == clock.now - timedelta(hours=1)

    async def test_a_full_page_inside_one_instant_is_not_passed(self, breach):
        """More than a page of verdicts sharing one created_at cannot be
        narrowed. The cursor stays behind them rather than dropping some."""
        verdicts = [_at(breach, f"vrd-{n:05d}", _T0) for n in range(_CORE_LIMIT_CAP)]
        core = _Core(verdicts)
        clock = _Clock(_T0 + timedelta(hours=3))
        module = _module(core, clock, expiry_threshold_days=0)

        for _ in range(4):
            await module.process_cycle()
            clock.now += timedelta(minutes=1)

        assert core.lineage_reads == []
        cursor = (await module.get_state()).get("cursor")
        assert cursor is None or _instant(cursor) <= _T0


class TestExpiryCap:
    async def test_expiries_are_capped_per_cycle_and_the_rest_follow(self, breach):
        verdicts = [_at(breach, f"vrd-{n:05d}", _T0 + timedelta(seconds=n)) for n in range(250)]
        core = _Core(verdicts)
        clock = _Clock(_T0 + timedelta(days=8))
        module = _module(core, clock, max_expiries_per_cycle=100)

        counts = []
        logged = []
        for _ in range(4):
            before = len(core.posts)
            with structlog.testing.capture_logs() as logs:
                await module.process_cycle()
            counts.append(len(core.posts) - before)
            logged.append([e for e in logs if e["event"] == "outcome_expiry_sweep"])
            clock.now += timedelta(minutes=1)

        assert counts == [100, 100, 50, 0]
        assert core.posted("expired") == [v["id"] for v in verdicts]
        assert [(e[0]["expired"], e[0]["capped"]) for e in logged[:3]] == [
            (100, True), (100, True), (50, False),
        ]
        assert logged[3] == []


# nthlayer-common api_client.py: a transport failure comes back as status_code
# 0, and 429 is one of _TRANSIENT_CODES, returned once the retries run out.
_NOT_AN_ANSWER = [503, 0, 429]


class TestCoreUnavailable:
    @pytest.mark.parametrize("status", _NOT_AN_ANSWER)
    async def test_an_unreadable_lineage_expires_nothing_and_is_retried(self, breach, status):
        core = _Core([_at(breach, "vrd-1", _T0), _at(breach, "vrd-2", _T0 + timedelta(minutes=1))])
        core.down_with = APIResult(ok=False, status_code=status, error="unavailable")
        clock = _Clock(_T0 + timedelta(days=8))
        module = _module(core, clock)

        core.lineage_down = True
        await module.process_cycle()
        assert core.posts == []

        core.lineage_down = False
        clock.now += timedelta(minutes=1)
        await module.process_cycle()
        assert core.posted("expired") == ["vrd-1", "vrd-2"]

    @pytest.mark.parametrize("expiring", [False, True])
    async def test_a_lineage_read_core_refuses_does_not_hold_the_verdicts_behind_it(self, breach, expiring):
        """A 4xx is the same answer every cycle (an id with a slash in it
        misses core's route and reads 404): the verdict is passed, not waited on."""
        core = _Core([_at(breach, "a/b", _T0), _at(breach, "vrd-2", _T0 + timedelta(minutes=1))])
        client = core.client()

        async def _descendants(verdict_id):
            if verdict_id == "a/b":
                return APIResult(ok=False, status_code=404, error="not_found")
            return await core.get_descendants(verdict_id)

        client.get_descendants = AsyncMock(side_effect=_descendants)
        clock = _Clock(_T0 + (timedelta(days=8) if expiring else timedelta(hours=2)))
        module = LearnOutcomeModule(client=client, clock=clock)

        await module.process_cycle()

        assert core.lineage_reads == ["vrd-2"]
        assert core.posted("expired") == (["vrd-2"] if expiring else [])
        state = await module.get_state()
        assert _instant(state["cursor"]) == clock.now - timedelta(hours=1)
        if expiring:
            assert _instant(state["expiry_cursor"]) == clock.now - timedelta(days=7)

    @pytest.mark.parametrize("status", _NOT_AN_ANSWER)
    async def test_a_post_core_did_not_answer_is_retried_next_cycle(self, breach, status):
        core = _Core([_at(breach, "vrd-1", _T0)])
        core.down_with = APIResult(ok=False, status_code=status, error="unavailable")
        core.add(_execution("vrd-exec", "vrd-1", _T0 + timedelta(minutes=5)))
        clock = _Clock(_T0 + timedelta(hours=2))
        module = _module(core, clock)

        core.outcome_down = True
        await module.process_cycle()
        assert core.signals == []

        core.outcome_down = False
        clock.now += timedelta(minutes=1)
        await module.process_cycle()
        assert core.posted("confirmed") == ["vrd-1"]
        assert len(core.signals) == 1

    async def test_a_resolution_core_refuses_does_not_hold_the_verdicts_behind_it(self, breach):
        """A 4xx other than 409 will be the same answer next cycle."""
        core = _Core([_at(breach, "vrd-1", _T0), _at(breach, "vrd-2", _T0 + timedelta(minutes=1))])
        core.add(_execution("vrd-exec-1", "vrd-1", _T0 + timedelta(minutes=2)))
        core.add(_execution("vrd-exec-2", "vrd-2", _T0 + timedelta(minutes=3)))
        client = core.client()
        answers = [APIResult(ok=False, status_code=422, error="validation_error")]

        async def _resolve(verdict_id, body):
            return answers.pop() if answers else await core.resolve_outcome(verdict_id, body)

        client.resolve_outcome = AsyncMock(side_effect=_resolve)
        clock = _Clock(_T0 + timedelta(hours=2))
        module = LearnOutcomeModule(client=client, clock=clock)

        await module.process_cycle()

        assert core.posted("confirmed") == ["vrd-2"]
        assert [s["data"]["verdict_id"] for s in core.signals] == ["vrd-2"]
        assert _instant((await module.get_state())["cursor"]) == clock.now - timedelta(hours=1)

    async def test_a_failed_query_leaves_both_cursors_where_they_were(self, breach):
        core = _Core([_at(breach, "vrd-1", _T0)])
        client = core.client()
        client.get_verdicts = AsyncMock(return_value=_unavailable())
        module = LearnOutcomeModule(client=client, clock=_Clock(_T0 + timedelta(days=8)))
        state = {"cursor": _T0.isoformat(), "expiry_cursor": (_T0 - timedelta(days=1)).isoformat()}
        await module.restore_state(state)

        await module.process_cycle()

        assert await module.get_state() == state


class TestState:
    async def test_both_cursors_round_trip(self, breach):
        core = _Core([_at(breach, "vrd-1", _T0)])
        clock = _Clock(_T0 + timedelta(days=8))
        module = _module(core, clock)
        await module.process_cycle()
        state = await module.get_state()

        assert _instant(state["cursor"]) == clock.now - timedelta(hours=1)
        assert _instant(state["expiry_cursor"]) == clock.now - timedelta(days=7)

        restored = _module(core, clock)
        await restored.restore_state(state)
        assert await restored.get_state() == state

    async def test_a_cursor_written_before_this_change_still_reads(self, breach):
        """The cursor used to be a verdict's created_at, as its producer wrote it."""
        core = _Core([_at(breach, "vrd-old", _T0), _at(breach, "vrd-new", _T0 + timedelta(hours=2))])
        module = _module(core, _Clock(_T0 + timedelta(hours=4)), expiry_threshold_days=0)
        await module.restore_state({"cursor": "2026-11-01T13:00:00Z"})

        await module.process_cycle()

        assert core.lineage_reads == ["vrd-new"]

    async def test_an_unreadable_cursor_is_dropped_not_sent_to_core(self, breach):
        core = _Core([_at(breach, "vrd-1", _T0)])
        module = _module(core, _Clock(_T0 + timedelta(hours=4)), expiry_threshold_days=0)
        await module.restore_state({"cursor": "not-a-time", "expiry_cursor": 7})

        await module.process_cycle()

        assert core.lineage_reads == ["vrd-1"]


class TestInputsThatCannotBeWorked:
    @pytest.mark.parametrize("cap", [0, -1])
    def test_an_expiry_cap_below_one_is_refused(self, cap):
        with pytest.raises(ValueError):
            LearnOutcomeModule(client=AsyncMock(), max_expiries_per_cycle=cap)

    @pytest.mark.parametrize("row", [{"type": "quality_breach"}, {"id": None}, "not-a-verdict"])
    async def test_a_row_without_an_id_is_passed_over(self, breach, row):
        core = _Core([_at(breach, "vrd-1", _T0)])
        client = core.client()
        client.get_verdicts = AsyncMock(return_value=_ok([core.verdicts["vrd-1"], row]))
        module = LearnOutcomeModule(client=client, clock=_Clock(_T0 + timedelta(days=8)))

        await module.process_cycle()

        assert core.lineage_reads == ["vrd-1"]
        assert core.posted("expired") == ["vrd-1"]

    # Core stores created_at as the producer sent it (server.py post_verdict
    # checks only that the field is there), so any string can come back.
    @pytest.mark.parametrize("stamp", ["2026-11-01T12:00:00", "2026-11-01T12:00:00Z"])
    async def test_a_created_at_without_an_offset_is_read_as_utc(self, breach, stamp):
        odd = {**breach, "id": "vrd-odd", "created_at": stamp}
        core = _Core([odd, _at(breach, "vrd-2", _T0 + timedelta(minutes=1))])
        client = core.client()
        client.get_verdicts = AsyncMock(return_value=_ok([core.verdicts["vrd-2"], odd]))
        module = LearnOutcomeModule(client=client, clock=_Clock(_T0 + timedelta(days=8)))

        await module.process_cycle()

        assert core.posted("expired") == ["vrd-odd", "vrd-2"]

    @pytest.mark.parametrize("stamp", ["yesterday", "", None, 20261101])
    async def test_a_created_at_that_is_not_a_time_is_never_expired_and_holds_nothing(self, breach, stamp):
        odd = {**breach, "id": "vrd-odd", "created_at": stamp}
        core = _Core([odd, _at(breach, "vrd-2", _T0)])
        client = core.client()
        client.get_verdicts = AsyncMock(return_value=_ok([core.verdicts["vrd-2"], odd]))
        module = LearnOutcomeModule(client=client, clock=_Clock(_T0 + timedelta(days=8)))

        await module.process_cycle()

        assert core.posted("expired") == ["vrd-2"]

    @pytest.mark.parametrize("body", [None, {}, {"verdicts": []}, "not json"])
    async def test_a_page_that_is_not_a_list_is_not_an_empty_window(self, breach, body):
        core = _Core([_at(breach, "vrd-1", _T0)])
        client = core.client()
        client.get_verdicts = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=body))
        module = LearnOutcomeModule(client=client, clock=_Clock(_T0 + timedelta(days=8)))
        state = {"cursor": _T0.isoformat(), "expiry_cursor": (_T0 - timedelta(days=1)).isoformat()}
        await module.restore_state(state)

        await module.process_cycle()

        assert await module.get_state() == state


class TestWhereASweepStops:
    """The cursor after a sweep that could not finish its page."""

    async def test_the_expiry_cap_leaves_the_cursor_at_the_first_verdict_not_expired(self, breach):
        stamps = [_T0, _T0 + timedelta(minutes=1), _T0 + timedelta(minutes=2)]
        core = _Core([_at(breach, f"vrd-{n}", at) for n, at in enumerate(stamps)])
        module = _module(core, _Clock(_T0 + timedelta(days=8)), max_expiries_per_cycle=2)

        await module.process_cycle()

        assert core.posted("expired") == ["vrd-0", "vrd-1"]
        assert _instant((await module.get_state())["expiry_cursor"]) == stamps[2]

    @pytest.mark.parametrize("expiring", [False, True])
    async def test_an_unreadable_lineage_leaves_the_cursor_at_that_verdict(self, breach, expiring):
        stamps = [_T0, _T0 + timedelta(minutes=1)]
        core = _Core([_at(breach, f"vrd-{n}", at) for n, at in enumerate(stamps)])
        client = core.client()

        async def _descendants(verdict_id):
            return _unavailable() if verdict_id == "vrd-1" else await core.get_descendants(verdict_id)

        client.get_descendants = AsyncMock(side_effect=_descendants)
        clock = _Clock(_T0 + (timedelta(days=8) if expiring else timedelta(hours=2)))
        module = LearnOutcomeModule(client=client, clock=clock)

        await module.process_cycle()

        state = await module.get_state()
        assert _instant(state["expiry_cursor" if expiring else "cursor"]) == stamps[1]

    @pytest.mark.parametrize("status", _NOT_AN_ANSWER)
    async def test_an_expiry_core_did_not_answer_is_retried_next_cycle(self, breach, status):
        core = _Core([_at(breach, "vrd-1", _T0)])
        core.down_with = APIResult(ok=False, status_code=status, error="unavailable")
        clock = _Clock(_T0 + timedelta(days=8))
        module = _module(core, clock)

        core.outcome_down = True
        await module.process_cycle()
        assert core.posts == []
        assert _instant((await module.get_state())["expiry_cursor"]) == _T0

        core.outcome_down = False
        clock.now += timedelta(minutes=1)
        await module.process_cycle()
        assert core.posted("expired") == ["vrd-1"]

    async def test_a_verdict_that_raises_is_passed_and_the_next_one_examined(self, breach):
        core = _Core([_at(breach, "vrd-1", _T0), _at(breach, "vrd-2", _T0 + timedelta(minutes=1))])
        client = core.client()

        async def _descendants(verdict_id):
            if verdict_id == "vrd-1":
                raise RuntimeError("not a client result")
            return await core.get_descendants(verdict_id)

        client.get_descendants = AsyncMock(side_effect=_descendants)
        clock = _Clock(_T0 + timedelta(days=8))
        module = LearnOutcomeModule(client=client, clock=clock)

        await module.process_cycle()

        assert core.posted("expired") == ["vrd-2"]
        assert _instant((await module.get_state())["expiry_cursor"]) == clock.now - timedelta(days=7)

    async def test_a_cursor_ahead_of_the_cutoff_is_not_moved_back(self, breach):
        """A clock that stepped back, or a threshold that was raised."""
        core = _Core([_at(breach, "vrd-1", _T0)])
        module = _module(core, _Clock(_T0 + timedelta(days=8)))
        state = {
            "cursor": (_T0 + timedelta(days=8)).isoformat(),
            "expiry_cursor": (_T0 + timedelta(days=2)).isoformat(),
        }
        await module.restore_state(state)

        await module.process_cycle()

        assert await module.get_state() == state
        assert core.queries == []

    async def test_only_an_expiry_core_recorded_counts_towards_the_cap(self, breach):
        """An expiry answered 409 was someone else's write, not one of this cycle's."""
        core = _Core([_at(breach, f"vrd-{n}", _T0 + timedelta(minutes=n)) for n in range(3)])
        client = core.client()
        answers = [APIResult(ok=False, status_code=409, error="duplicate")]

        async def _resolve(verdict_id, body):
            return answers.pop() if answers else await core.resolve_outcome(verdict_id, body)

        client.resolve_outcome = AsyncMock(side_effect=_resolve)
        module = LearnOutcomeModule(
            client=client, clock=_Clock(_T0 + timedelta(days=8)), max_expiries_per_cycle=2
        )

        await module.process_cycle()

        assert core.posted("expired") == ["vrd-1", "vrd-2"]

    async def test_a_verdict_exactly_at_the_expiry_age_is_expired(self, breach):
        core = _Core([_at(breach, "vrd-1", _T0)])
        module = _module(core, _Clock(_T0 + timedelta(days=7)))

        await module.process_cycle()

        assert core.posted("expired") == ["vrd-1"]


class TestNarrowingWithNoCursor:
    async def test_a_first_run_over_a_long_backlog_starts_at_its_old_end(self, breach):
        """30 days of verdicts and no cursor: one step back of a day leaves the
        page full, so the step has to grow until the window's end is early
        enough. The first window that is not full may be empty (it ends
        before the backlog begins); the cycle after it reads from there."""
        step = timedelta(days=30) / 3000
        verdicts = [_at(breach, f"vrd-{n:05d}", _T0 + n * step) for n in range(3000)]
        core = _Core(verdicts)
        clock = _Clock(_T0 + timedelta(days=31))
        module = _module(core, clock, expiry_threshold_days=0)

        for _ in range(2):
            await module.process_cycle()
            clock.now += timedelta(minutes=1)

        assert 0 < len(core.lineage_reads) < _CORE_LIMIT_CAP
        assert core.lineage_reads == [v["id"] for v in verdicts[:len(core.lineage_reads)]]

    async def test_a_full_page_at_the_cursors_own_instant_is_given_up_on(self, breach):
        """Nothing left to halve: the page is not processed and the cursor stays."""
        verdicts = [_at(breach, f"vrd-{n:05d}", _T0) for n in range(_CORE_LIMIT_CAP)]
        core = _Core(verdicts)
        module = _module(core, _Clock(_T0 + timedelta(hours=3)), expiry_threshold_days=0)
        await module.restore_state({"cursor": _T0.isoformat()})

        with structlog.testing.capture_logs() as logs:
            await module.process_cycle()

        assert core.lineage_reads == []
        assert await module.get_state() == {"cursor": _T0.isoformat()}
        assert [e["event"] for e in logs] == ["outcome_window_unnarrowable"]


class TestLineageSignals:
    @pytest.mark.parametrize("overridden_first", [True, False])
    async def test_an_overridden_descendant_outranks_an_execution(self, breach, overridden_first):
        """An execution that was overridden is a divergence, not a confirmation."""
        core = _Core([_at(breach, "vrd-1", _T0)])
        names = ["vrd-over", "vrd-exec"] if overridden_first else ["vrd-exec", "vrd-over"]
        for n, name in enumerate(names):
            core.add(_execution(name, "vrd-1", _T0 + timedelta(minutes=n + 1)))
        core.override_in_place("vrd-over")
        module = _module(core, _Clock(_T0 + timedelta(hours=2)))

        await module.process_cycle()

        assert core.posted("overridden") == ["vrd-1"]
        assert core.posts[0][1]["path"] == "divergence"
        assert core.signals[0]["data"]["observed_outcome"] == "overridden"

    @pytest.mark.parametrize("body", [None, {}, "not json"])
    async def test_a_lineage_body_that_is_not_a_list_is_unreadable_not_silent(self, breach, body):
        """Expiring on it would record that nothing resolved a verdict whose
        lineage was never read."""
        core = _Core([_at(breach, "vrd-1", _T0)])
        client = core.client()
        client.get_descendants = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=body))
        module = LearnOutcomeModule(client=client, clock=_Clock(_T0 + timedelta(days=8)))

        await module.process_cycle()

        assert core.posts == []
        assert _instant((await module.get_state())["expiry_cursor"]) == _T0
