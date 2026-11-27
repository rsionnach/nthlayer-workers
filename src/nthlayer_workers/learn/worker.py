"""Learn worker modules — outcome resolution and retrospective generation.

Two modules implementing WorkerModule protocol:
- LearnOutcomeModule: continuous verdict outcome resolution + calibration signals (60s)
- LearnRetrospectiveModule: incident retrospective triggered by correlation_snapshot (30s)
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

import structlog
from nthlayer_common.api_client import CoreAPIClient
from nthlayer_common.cloudevents import wrap_assessment
from nthlayer_common.manifest import extract_declared_dependencies

from nthlayer_workers.learn._trigger import resolve_trigger_service

logger = structlog.get_logger()

# Calibration delta: observed_outcome_score mapping
_OUTCOME_SCORES = {
    "confirmed": 1.0,
    "overridden": 0.0,
    "partial": 0.5,
}

_PENDING = "pending"

# Verdict types that record a resolution rather than a judgment. Core builds
# an outcome_resolution verdict with no ``outcome`` key (POST
# /verdicts/{id}/outcome), so it would otherwise read as pending and an expiry
# would resolve the resolution [opensrm-eel4].
_RESOLUTION_RECORD_TYPES = frozenset({"outcome_resolution"})


def _raw_outcome_status(verdict: dict) -> Any:
    """``outcome.status`` as stored, or None when the verdict carries none.

    A verdict may have no ``outcome`` key (measure's quality_breach and
    autonomy_change verdicts), a null ``outcome`` or an ``outcome`` without a
    status; none of these may raise.
    """
    outcome = verdict.get("outcome")
    if not isinstance(outcome, dict):
        return None
    return outcome.get("status")


def _outcome_status(verdict: dict) -> Any:
    """The verdict's outcome status, with a missing one read as pending.

    This is how core reads it (``Store.update_verdict_outcome`` coalesces an
    absent ``outcome.status`` to pending), and core stores a verdict as sent,
    so learn must agree or it never resolves a verdict whose producer wrote no
    outcome [opensrm-eel4].
    """
    status = _raw_outcome_status(verdict)
    return _PENDING if status is None else status


def _resolves(descendant: dict, verdict_id: str) -> bool:
    """Whether ``descendant`` is the resolution record of ``verdict_id`` itself.

    Core's lineage is transitive, so the descendants of a verdict include the
    resolution records of its children. Expiring an autonomy_change must not
    read as a confirmation of the quality_breach above it [opensrm-eel4].
    """
    if descendant.get("type") not in _RESOLUTION_RECORD_TYPES:
        return False
    parent_ids = descendant.get("parent_ids")
    return isinstance(parent_ids, list) and verdict_id in parent_ids


# ---------------------------------------------------------------------------
# LearnOutcomeModule — continuous verdict outcome resolution
# ---------------------------------------------------------------------------

# The most verdicts core returns for one query (nthlayer-core server.py
# LIMIT_CAP). Core serves newest first, so a page this long has dropped the
# oldest of its window. order=asc in core would replace the narrowing
# below [opensrm-6imd].
_PAGE_LIMIT = 1000
# Queries one sweep may spend narrowing a truncated window.
_MAX_NARROWINGS = 64
# With no cursor there is no lower bound to halve towards, so the window's end
# is stepped back from the cutoff by a span that starts here and doubles.
_FIRST_STEP_BACK = timedelta(days=1)

# Core's answer to a write whose id it already holds.
_HTTP_CONFLICT = 409
_HTTP_BAD_REQUEST = 400
_HTTP_TOO_MANY_REQUESTS = 429
_HTTP_SERVER_ERROR = 500

_EXPIRED_RESOLUTION = {
    "outcome_status": "expired",
    "resolution": "No outcome signal within threshold",
}


class _Lineage(Enum):
    """What a verdict's descendants say about its outcome."""

    UNREADABLE = "unreadable"  # core did not answer
    REFUSED = "refused"  # core will not answer for this verdict
    RESOLVED = "resolved"  # core already holds this verdict's resolution record
    SIGNAL = "signal"  # a descendant resolves it; the resolution is not recorded yet
    SILENT = "silent"  # nothing resolves it


class _Write(Enum):
    """How core answered a resolution."""

    RECORDED = "recorded"
    NOT_NEEDED = "not_needed"  # already resolved, or refused for good
    UNAVAILABLE = "unavailable"


class _Step(Enum):
    """How one verdict's examination ended."""

    DONE = "done"
    EXPIRED = "expired"
    HALT = "halt"  # core is unavailable: stop here and come back to this verdict


def _is_refusal(status_code: int) -> bool:
    """Whether core's answer for this verdict will be the same next cycle.

    A 4xx is, except 429: the client retries it as transient and hands it
    back when the retries run out (``api_client._TRANSIENT_CODES``). A
    transport failure reads as status 0.
    """
    if status_code == _HTTP_TOO_MANY_REQUESTS:
        return False
    return _HTTP_BAD_REQUEST <= status_code < _HTTP_SERVER_ERROR


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class LearnOutcomeModule:
    """Continuous background maintenance of verdict outcome state.

    Each verdict is examined at two ages, by two sweeps with a cursor each
    [opensrm-lpbi]:

    - the resolution sweep, when the verdict passes ``minimum_resolution_age``:
      a descendant that resolves it (an execution, an override) is recorded as
      its outcome and a calibration_signal is emitted;
    - the expiry sweep, when it passes ``expiry_threshold``: its lineage is
      read again, a signal that arrived late is recorded as above, and a
      verdict that still has none is expired.

    A sweep reads the window between its cursor and its age cutoff, oldest
    first, and moves the cursor to the end of what it read. Core holds the
    record of what is resolved: a verdict whose own ``outcome_resolution``
    child exists is left alone.

    Calibration signals feed measure module's self-calibration (v2).
    Expired verdicts don't produce signals — absence of data is not
    a quality signal.
    """

    client: CoreAPIClient
    expiry_threshold_days: int = 7
    minimum_resolution_age_hours: int = 1
    # The first expiry sweep meets every pending verdict older than the
    # threshold; each expiry is a write to core.
    max_expiries_per_cycle: int = 100
    clock: Callable[[], datetime] = _utc_now
    _cursor: datetime | None = None
    _expiry_cursor: datetime | None = None

    def __post_init__(self) -> None:
        # A cap of zero would stop the expiry sweep at its first candidate
        # every cycle. Expiry is switched off with expiry_threshold_days.
        if self.max_expiries_per_cycle < 1:
            raise ValueError("max_expiries_per_cycle must be at least 1")

    @property
    def name(self) -> str:
        return "learn.outcome"

    async def restore_state(self, state: dict | None) -> None:
        if state:
            # A cursor that does not read as a time is dropped: the sweep then
            # starts from the beginning, which costs reads and loses nothing.
            self._cursor = _parse_timestamp(state.get("cursor"))
            self._expiry_cursor = _parse_timestamp(state.get("expiry_cursor"))

    async def process_cycle(self) -> None:
        now = self.clock()
        # Verdicts the expiry sweep examined this cycle. On a backlog both
        # windows hold them, and one lineage read per cycle is enough.
        examined: set[str] = set()

        if self.expiry_threshold_days > 0:
            self._expiry_cursor = await self._sweep(
                self._expiry_cursor,
                now - timedelta(days=self.expiry_threshold_days),
                now=now, expire=True, examined=examined,
            )

        # Minimum age floor: downstream signals may not have arrived for a
        # younger verdict, and a look that finds nothing is not repeated
        # until the expiry age.
        self._cursor = await self._sweep(
            self._cursor,
            now - timedelta(hours=self.minimum_resolution_age_hours),
            now=now, expire=False, examined=examined,
        )

    async def _sweep(
        self,
        cursor: datetime | None,
        cutoff: datetime,
        *,
        now: datetime,
        expire: bool,
        examined: set[str],
    ) -> datetime | None:
        """Examine the verdicts created from ``cursor`` to ``cutoff``, oldest first.

        Returns the sweep's new cursor: the end of the window it read, the
        verdict it had to stop at, or ``cursor`` unchanged when it read nothing.
        """
        if cursor is not None and cursor >= cutoff:
            return cursor
        window = await self._oldest_page(cursor, cutoff)
        if window is None:
            return cursor
        page, window_end = window

        expired = 0
        capped = False
        stopped_at: dict | None = None
        for verdict in page:
            verdict_id = verdict.get("id") if isinstance(verdict, dict) else None
            if not isinstance(verdict_id, str):
                continue  # nothing core could be asked about
            if verdict.get("type") in _RESOLUTION_RECORD_TYPES:
                continue
            # POST /verdicts/{id}/override sets the original's outcome in place.
            if _outcome_status(verdict) != _PENDING:
                continue
            if verdict_id in examined:
                continue
            # The verdict's own age decides, not the window that was asked for.
            if expire and not self._is_past_expiry(verdict, now):
                continue
            if expire and expired >= self.max_expiries_per_cycle:
                capped = True
                stopped_at = verdict
                break

            try:
                step = await self._examine(verdict, expire=expire)
            except Exception:
                # One verdict that cannot be examined must not hold the rest.
                logger.warning("outcome_resolution_failed", verdict_id=verdict_id)
                step = _Step.DONE
            if step is _Step.HALT:
                stopped_at = verdict
                break
            examined.add(verdict_id)
            if step is _Step.EXPIRED:
                expired += 1

        if expired or capped:
            logger.info("outcome_expiry_sweep", expired=expired, capped=capped)
        if stopped_at is None:
            return window_end
        # The bounds core applies are inclusive, so the verdict stopped at is
        # read again next cycle. So are any that share its created_at, and
        # those already resolved are skipped on their resolution record.
        resume_at = _parse_timestamp(stopped_at.get("created_at"))
        if resume_at is None or (cursor is not None and resume_at <= cursor):
            return cursor
        return resume_at

    async def _oldest_page(
        self, cursor: datetime | None, cutoff: datetime
    ) -> tuple[list[dict], datetime] | None:
        """The oldest untruncated page from ``cursor``, oldest first, and where it ends.

        Core returns newest first and at most ``_PAGE_LIMIT``, so a full page
        has dropped the oldest verdicts of its window. The window's end is
        then brought down towards the cursor until the page is not full: the
        verdicts next to the cursor are never passed over. None when core did
        not answer or the window could not be narrowed.
        """
        window_end = cutoff
        step_back = _FIRST_STEP_BACK
        for _ in range(_MAX_NARROWINGS):
            result = await self.client.get_verdicts(
                created_after=cursor.isoformat() if cursor else None,
                created_before=window_end.isoformat(),
                limit=_PAGE_LIMIT,
            )
            if not result.ok:
                logger.warning("outcome_query_failed", status_code=result.status_code)
                return None
            page = result.data
            if not isinstance(page, list):
                # Not a page of verdicts: reading it as an empty window would
                # move the cursor past everything in it.
                logger.warning("outcome_query_unreadable", status_code=result.status_code)
                return None
            if len(page) < _PAGE_LIMIT:
                return list(reversed(page)), window_end

            if cursor is None:
                try:
                    window_end = cutoff - step_back
                except OverflowError:
                    break
                step_back *= 2
            elif window_end <= cursor:
                break  # a full page inside one instant
            else:
                window_end = cursor + (window_end - cursor) / 2

        logger.error(
            "outcome_window_unnarrowable",
            cursor=cursor.isoformat() if cursor else None,
            window_end=window_end.isoformat(),
        )
        return None

    async def _examine(self, verdict: dict, *, expire: bool) -> _Step:
        """Read one pending verdict's lineage and record what it shows."""
        lineage, resolution = await self._read_lineage(verdict["id"])
        if lineage is _Lineage.UNREADABLE:
            return _Step.HALT
        if lineage in (_Lineage.RESOLVED, _Lineage.REFUSED):
            return _Step.DONE

        if resolution is not None:
            write = await self._record(verdict, resolution)
            if write is _Write.RECORDED:
                await self._emit_calibration_signal(verdict, resolution)
            return _Step.HALT if write is _Write.UNAVAILABLE else _Step.DONE

        if not expire:
            return _Step.DONE
        write = await self._record(verdict, dict(_EXPIRED_RESOLUTION))
        if write is _Write.UNAVAILABLE:
            return _Step.HALT
        return _Step.EXPIRED if write is _Write.RECORDED else _Step.DONE

    async def _read_lineage(self, verdict_id: str) -> tuple[_Lineage, dict | None]:
        """What the verdict's descendants say, and the resolution to record if any.

        The verdict's own resolution record is looked for first: core never
        marks the original, so that child is the only sign it is resolved, and
        a resolved verdict is not resolved again whatever else descends from it.
        Then, in priority order: an overridden descendant (divergence: an
        execution that was overridden is not a confirmation), an execution
        (lineage). Core's lineage is transitive, so both may sit under a child.
        """
        descendants = await self.client.get_descendants(verdict_id)
        if not descendants.ok:
            # A 4xx is core's answer for this verdict and will be the same
            # next cycle; waiting on it would hold every verdict behind it.
            refused = _is_refusal(descendants.status_code)
            logger.warning(
                "outcome_lineage_refused" if refused else "outcome_lineage_unreadable",
                verdict_id=verdict_id,
                status_code=descendants.status_code,
            )
            return (_Lineage.REFUSED if refused else _Lineage.UNREADABLE), None
        found = descendants.data if isinstance(descendants.data, list) else []

        if any(_resolves(desc, verdict_id) for desc in found):
            return _Lineage.RESOLVED, None

        overridden = next((d for d in found if _outcome_status(d) == "overridden"), None)
        if overridden is not None:
            return _Lineage.SIGNAL, {
                "outcome_status": "overridden",
                "resolution": f"Overridden by {overridden.get('id')}",
                "path": "divergence",
            }
        execution = next((d for d in found if d.get("type") == "execution"), None)
        if execution is not None:
            return _Lineage.SIGNAL, {
                "outcome_status": "confirmed",
                "resolution": f"Resolved by downstream execution verdict {execution.get('id')}",
                "path": "lineage",
            }
        # Calibration sampling is a v1.5 stub; an external signal arrives as a
        # verdict naming this one in parent_ids, so the lineage read covers it.
        return _Lineage.SILENT, None

    async def _record(self, verdict: dict, resolution: dict) -> _Write:
        """POST the resolution to core and say how it was answered."""
        verdict_id = verdict["id"]
        submit = await self.client.resolve_outcome(verdict_id, resolution)
        if submit.ok:
            return _Write.RECORDED
        if submit.status_code == _HTTP_CONFLICT:
            # Core holds this verdict's resolution record already: another
            # writer got there between the lineage read and this POST.
            return _Write.NOT_NEEDED
        if _is_refusal(submit.status_code):
            # Core will give the same answer next cycle; waiting on it would
            # hold every verdict behind this one.
            logger.warning(
                "outcome_resolution_refused",
                verdict_id=verdict_id,
                status_code=submit.status_code,
            )
            return _Write.NOT_NEEDED
        logger.warning(
            "outcome_resolution_unavailable",
            verdict_id=verdict_id,
            status_code=submit.status_code,
        )
        return _Write.UNAVAILABLE

    def _is_past_expiry(self, verdict: dict, now: datetime) -> bool:
        """Check if a verdict has exceeded the expiry threshold."""
        if self.expiry_threshold_days <= 0:
            return False  # 0 or negative threshold disables expiry
        # Core stores created_at as its producer sent it: one with no offset
        # is read as UTC, and one that is not a time is never past expiry.
        created = _parse_timestamp(verdict.get("created_at"))
        if created is None:
            return False
        return now - created >= timedelta(days=self.expiry_threshold_days)

    async def _emit_calibration_signal(self, verdict: dict, resolution: dict) -> None:
        """Emit calibration_signal assessment for a resolved verdict."""
        judgment = verdict.get("judgment", {})
        confidence = judgment.get("confidence")
        outcome_status = resolution.get("outcome_status", "")

        # Compute calibration delta
        delta = None
        if confidence is not None and outcome_status in _OUTCOME_SCORES:
            delta = abs(confidence - _OUTCOME_SCORES[outcome_status])

        now = datetime.now(UTC)
        assessment = {
            "id": f"cal-{verdict.get('id', 'unknown')}-{uuid.uuid4().hex[:8]}",
            "created_at": now.isoformat(),
            "kind": "calibration_signal",
            "service": verdict.get("service", "unknown"),
            "data": {
                "verdict_id": verdict.get("id"),
                "verdict_type": verdict.get("type"),
                "expressed_confidence": confidence,
                "observed_outcome": outcome_status,
                "calibration_delta": round(delta, 4) if delta is not None else None,
                "resolution_path": resolution.get("path", "unknown"),
                "producer_system": verdict.get("producer", {}).get("system"),
            },
        }
        result = await self.client.submit_assessment(wrap_assessment(assessment, component="learn"))
        if not result.ok:
            logger.warning("calibration_signal_submit_failed", verdict_id=verdict.get("id"))

    async def get_state(self) -> dict:
        state: dict[str, Any] = {}
        if self._cursor:
            state["cursor"] = self._cursor.isoformat()
        if self._expiry_cursor:
            state["expiry_cursor"] = self._expiry_cursor.isoformat()
        return state


# ---------------------------------------------------------------------------
# LearnRetrospectiveModule — incident retrospective from correlation snapshots
# ---------------------------------------------------------------------------


# The verdict type core gives the child it creates when an outcome is
# resolved (POST /verdicts/{id}/outcome): id ``out-<id>``, parent_ids [<id>].
_OUTCOME_RESOLUTION = "outcome_resolution"
# The two of core's case states that are not "resolved". Cases are asked
# for by state, so a resolved one is never among those returned.
_OPEN_CASE_STATES = ("pending", "leased")
_CASE_PAGE_LIMIT = 100



def retrospective_id(snapshot: dict) -> str:
    """The id of the one retrospective a snapshot gets."""
    return f"retro-{snapshot.get('id')}"


# How a retrospective came to be written.
CLOSED_BY_QUIET = "quiet"
CLOSED_BY_MAX_AGE = "max_age"


@dataclass(frozen=True)
class IncidentChain:
    """The decisions of one incident, oldest first, and which of them have
    an outcome.

    ``resolved_ids`` says only that core holds an outcome child for the
    decision, never which outcome it carries.
    """

    decisions: tuple[dict, ...]
    resolved_ids: frozenset[str]

    @property
    def ids(self) -> frozenset[str]:
        return frozenset(v["id"] for v in self.decisions)

    @property
    def newest_created_at(self) -> datetime | None:
        """When the last decision joined the chain, by core's timestamp."""
        stamps = [t for v in self.decisions if (t := _parse_timestamp(v.get("created_at")))]
        return max(stamps, default=None)


async def fetch_incident_chain(client: CoreAPIClient, snapshot: dict) -> IncidentChain | None:
    """The verdict chain of the incident a correlation snapshot opened, by
    lineage; None when core could not be read (try again next cycle).

    The chain is the snapshot's trigger verdicts (``data.parent_ids``) plus
    every descendant of the snapshot id. That reaches respond's verdicts
    because respond's first verdict lists the snapshot id in its
    ``parent_ids`` and each later one chains to the one before, and because
    core answers ``GET /verdicts/{id}/descendants`` for any ancestor id, an
    assessment's included: its lineage table has no foreign key. It is
    exactly this incident: a query by service and time also returns other
    incidents on the service, and misses everything respond does, since
    respond acts after the snapshot's window has closed [opensrm-91pa].

    Outcome resolutions (the ``out-<id>`` child core creates when an outcome
    is resolved) are not decisions and are left out of the chain; a decision
    that has one is listed in ``resolved_ids``. A trigger's outcome child
    descends from the trigger, not from the snapshot, so each trigger's
    descendants are read too, for that child alone.

    THIS IS THE ONE PLACE THAT KNOWS HOW AN INCIDENT'S VERDICTS ARE FOUND.
    Core pins the behaviour it leans on in
    ``test_descendants_of_an_assessment_id_returns_the_chain_that_names_it``
    (opensrm-brms); the three-tier harness is to assert the result end to
    end (opensrm-mtxr). When opensrm-7t3q's typed relations land, this
    function, ``tests/learn/test_incident_chain.py`` and those two change
    together.
    """
    snapshot_id = snapshot.get("id")
    data = snapshot.get("data") or {}
    trigger_ids = [p for p in (data.get("parent_ids") or []) if isinstance(p, str) and p]

    decisions: dict[str, dict] = {}
    outcome_children: list[dict] = []

    def take(verdicts: Any, *, outcomes_only: bool = False) -> None:
        for verdict in verdicts if isinstance(verdicts, list) else []:
            if not isinstance(verdict, dict) or not verdict.get("id"):
                continue
            if verdict.get("type") == _OUTCOME_RESOLUTION:
                outcome_children.append(verdict)
            elif not outcomes_only:
                decisions[verdict["id"]] = verdict

    if snapshot_id:
        descendants = await client.get_descendants(snapshot_id)
        if not descendants.ok:
            return None
        take(descendants.data)

    for trigger_id in trigger_ids:
        trigger = await client.get_verdict(trigger_id)
        if trigger.ok and isinstance(trigger.data, dict):
            decisions[trigger_id] = trigger.data
        elif trigger.status_code != 404:
            return None
        else:
            # Pruned by retention, or never stored: the chain goes on
            # without it.
            continue
        descendants = await client.get_descendants(trigger_id)
        if not descendants.ok:
            return None
        take(descendants.data, outcomes_only=True)

    resolved = {
        parent
        for child in outcome_children
        for parent in (child.get("parent_ids") or [])
        if parent in decisions
    }
    ordered = sorted(decisions.values(), key=lambda v: str(v.get("created_at") or ""))
    return IncidentChain(decisions=tuple(ordered), resolved_ids=frozenset(resolved))


def incident_service(snapshot: dict) -> str:
    """The service respond files the snapshot's incident, and so its case,
    under: the correlator's grouping anchor, else the first affected
    service, else the placeholder. The same rule as respond's
    ``open_from_snapshot``; ``tests/learn/test_incident_chain.py`` holds the
    two together."""
    data = snapshot.get("data") or {}
    domain = data.get("domain") or {}
    return (
        domain.get("service")
        or next(iter(data.get("affected_services") or []), None)
        or "unknown"
    )


async def has_open_case(
    client: CoreAPIClient, anchor_ids: frozenset[str], service: str
) -> bool | None:
    """Whether core holds an unresolved case for *service* whose underlying
    verdict is one of *anchor_ids*; None when that cannot be told.

    It cannot be told when a read fails, or when a page of the service's
    open cases comes back full, since the anchoring case may be beyond it.
    Reading one service's cases keeps that page from filling with the rest
    of the fleet's.
    """
    for state in _OPEN_CASE_STATES:
        result = await client.get_cases(state=state, service=service, limit=_CASE_PAGE_LIMIT)
        if not result.ok:
            return None
        cases = result.data if isinstance(result.data, list) else []
        for case in cases:
            if isinstance(case, dict) and case.get("underlying_verdict") in anchor_ids:
                return True
        if len(cases) >= _CASE_PAGE_LIMIT:
            return None
    return False


def retrospective_closure(
    *,
    now: datetime,
    snapshot_created_at: datetime | None,
    chain: IncidentChain,
    open_case: bool | None,
    quiet: timedelta,
    max_age: timedelta,
) -> str | None:
    """How a pending snapshot's retrospective closes now, or None to wait.

    ``max_age``: the snapshot is older than the maximum age. It is written
    whatever the state of the incident, so it may describe an open one.

    ``quiet``: no unresolved case in core anchors on the chain, and nothing
    has joined the chain for the quiet period. The clock runs from core's
    timestamps (the newest decision, or the snapshot itself when it is newer
    or the chain is empty), never from when learn first saw the snapshot, so
    a restart does not reset it. An open case, or one that cannot be ruled
    out (``open_case`` None), waits.

    A snapshot whose ``created_at`` cannot be read has no age to measure and
    closes at once as ``max_age``, so that it is not held forever.
    """
    if snapshot_created_at is None or now - snapshot_created_at >= max_age:
        return CLOSED_BY_MAX_AGE
    if open_case is not False:
        return None
    newest = chain.newest_created_at
    last_activity = snapshot_created_at if newest is None else max(newest, snapshot_created_at)
    return CLOSED_BY_QUIET if now - last_activity >= quiet else None


def _parse_timestamp(value: Any) -> datetime | None:
    """An ISO 8601 timestamp as an aware datetime, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass
class LearnRetrospectiveModule:
    """Write one retrospective per correlation snapshot, once its incident
    has run its course.

    A new snapshot is held as pending. Each cycle its verdict chain is read
    again (``fetch_incident_chain``), and the retrospective is written when
    ``retrospective_closure`` says so: the chain has gone quiet with no open
    case anchored on it, or the snapshot has reached the maximum age. Written
    at once, as it used to be, the retrospective preceded everything respond
    did [opensrm-91pa].

    The cursor and the pending snapshots are in ``get_state``, but nothing
    here relies on getting them back: core keeps none of a worker's state
    today (opensrm-youu). After a restart the module reads core's newest
    page of snapshots again, and the quiet clock runs from core's
    timestamps. No snapshot gets a second retrospective, because a
    snapshot's retrospective always has the same id and core refuses a
    duplicate. What a restart can lose is a pending snapshot that has
    fallen off that page.

    A snapshot whose chain cannot be read stays pending, and is given up
    (``retrospective_abandoned``) once it is twice the maximum age old.

    Known limit: if respond does not act within the quiet period (it is
    down, or slow), there is no case yet to hold the incident open, and the
    retrospective closes as ``quiet`` with the triggers alone.

    Retrospectives are narrative: "What happened during this incident?"
    They describe decisions and events, not whether decisions were correct.
    Most verdicts in a fresh chain won't have outcomes resolved yet —
    that's fine. Outcome resolution is the learn module's separate
    calibration output (LearnOutcomeModule).
    """

    client: CoreAPIClient
    quiet_minutes: float = 10
    max_age_hours: float = 24
    # The newest snapshot taken into ``_pending``; older ones are not looked
    # at again.
    _cursor: str | None = None
    # Snapshots with no retrospective yet, by id, as core returned them.
    _pending: dict[str, dict] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return "learn.retrospective"

    async def restore_state(self, state: dict | None) -> None:
        if state:
            self._cursor = state.get("cursor")
            pending = state.get("pending")
            if isinstance(pending, dict):
                self._pending = {
                    sid: snap for sid, snap in pending.items()
                    if isinstance(sid, str) and isinstance(snap, dict)
                }

    async def process_cycle(self) -> None:
        await self._take_new_snapshots()

        now = datetime.now(UTC)
        for snapshot_id, snapshot in list(self._pending.items()):
            chain = await fetch_incident_chain(self.client, snapshot)
            if chain is None:
                # Never written from half a chain. But not held forever
                # either: at twice the maximum age it is given up.
                created = _parse_timestamp(snapshot.get("created_at"))
                max_age = timedelta(hours=self.max_age_hours)
                if created is None or now - created >= 2 * max_age:
                    logger.error("retrospective_abandoned", snapshot_id=snapshot_id)
                    del self._pending[snapshot_id]
                else:
                    logger.warning("retrospective_chain_unreadable", snapshot_id=snapshot_id)
                continue
            closed_by = retrospective_closure(
                now=now,
                snapshot_created_at=_parse_timestamp(snapshot.get("created_at")),
                chain=chain,
                # Respond anchors an incident's case on a trigger verdict
                # (the first breach id), which is in the chain.
                open_case=await has_open_case(
                    self.client, chain.ids, incident_service(snapshot)
                ),
                quiet=timedelta(minutes=self.quiet_minutes),
                max_age=timedelta(hours=self.max_age_hours),
            )
            if closed_by is None:
                continue
            # A failed submission leaves the snapshot pending for next cycle.
            if await self._generate_retrospective(snapshot, chain, closed_by):
                del self._pending[snapshot_id]

    async def _take_new_snapshots(self) -> None:
        """Move snapshots newer than the cursor, and not yet written up, into
        ``_pending``.

        A process that starts with no state, which is every process until
        core keeps worker state (opensrm-youu), takes up the snapshots core's
        newest page still holds: the pending ones are pending again. The
        finished ones are skipped when core's newest page of retrospectives
        names them. That page can miss one, since retrospectives are not
        written in snapshot order, and it does not need to be complete: a
        snapshot taken up needlessly is refused by core when it is
        submitted, because its retrospective has the same id
        (``retrospective_id``). The read only saves the work in between.
        """
        result = await self.client.get_assessments(kind="correlation_snapshot")
        if not result.ok or not result.data:
            return
        candidates: list[dict] = []
        for snapshot in result.data:
            created = snapshot.get("created_at", "")
            if self._cursor and created <= self._cursor:
                continue
            snapshot_id = snapshot.get("id")
            if not snapshot_id:
                logger.warning("retrospective_snapshot_without_id", created_at=created)
                continue
            if snapshot_id not in self._pending:
                candidates.append(snapshot)
        if candidates:
            # A failed read skips nothing: core still refuses a second
            # retrospective for a snapshot that has one.
            written = await self._written_snapshot_ids() or set()
            for snapshot in candidates:
                if snapshot["id"] not in written:
                    self._pending[snapshot["id"]] = snapshot
        newest = max(
            (s.get("created_at", "") for s in result.data), default=""
        )
        if newest and (not self._cursor or newest > self._cursor):
            self._cursor = newest

    async def _written_snapshot_ids(self) -> set[str] | None:
        """The snapshots core's newest page of retrospectives covers; None
        when it cannot be read."""
        result = await self.client.get_assessments(kind="retrospective")
        if not result.ok:
            return None
        retrospectives = result.data if isinstance(result.data, list) else []
        return {
            snapshot_id
            for retro in retrospectives
            if isinstance(retro, dict)
            and (snapshot_id := (retro.get("data") or {}).get("correlation_snapshot_id"))
        }

    async def _generate_retrospective(
        self, snapshot: dict, incident: IncidentChain, closed_by: str
    ) -> bool:
        """Generate and submit a retrospective assessment. Returns True on success."""
        snapshot_data = snapshot.get("data", {})
        domain = snapshot_data.get("domain", {})
        # The service the retrospective is filed under: the snapshot's own,
        # as before. ``incident_service`` is a different question (which
        # service respond filed the case under) and has respond's rule; the
        # two agree whenever the snapshot names its domain's service, which
        # correlate's always do.
        service = snapshot.get("service", domain.get("service", "unknown"))

        # opensrm-dpws: resolve trigger_service via correlation-first /
        # snapshot-service-fallback precedence. The snapshot's
        # data.domain.service IS the correlator's grouping anchor; the
        # top-level service field is the same value emitted at submit
        # time but kept independent for resilience.
        trigger_service = resolve_trigger_service(
            [domain.get("service")],
            snapshot.get("service"),
        )

        chain = list(incident.decisions)

        # Build timeline
        timeline = _build_chain_timeline(chain)

        # A decision counts as resolved when core holds an outcome child for
        # it. Its own ``outcome`` field is not read here: core never updates
        # it. (A timeline entry's ``outcome`` is still that field as the
        # producer wrote it, so it can lag the coverage figures.)
        resolved_count = len(incident.resolved_ids)
        pending_count = len(chain) - resolved_count

        # Extract root cause from correlation data
        root_cause = snapshot_data.get("correlation_groups", [{}])[0] if snapshot_data.get("correlation_groups") else None

        # opensrm-dpws: declared_dependencies_by_service — populate only
        # when the trigger's own manifest is in the API result.
        # _add_dependency_recommendations reads declared_map.get(trigger)
        # so non-trigger gaps are harmless; trigger gap → over-broad recs.
        declared_dependencies_by_service: dict[str, list[str]] | None = None
        if trigger_service:
            manifests_result = await self.client.get_manifests()
            if not manifests_result.ok:
                logger.warning(
                    "learn_manifest_fetch_failed",
                    error=manifests_result.error,
                )
            elif not manifests_result.data:
                logger.info("learn_manifest_catalogue_empty")
            else:
                manifest_names = {
                    m.get("name") for m in manifests_result.data if m.get("name")
                }
                if trigger_service not in manifest_names:
                    logger.warning(
                        "learn_trigger_manifest_absent",
                        service=trigger_service,
                    )
                else:
                    # Trigger-narrow per design § 3.4: downstream
                    # _add_dependency_recommendations reads
                    # declared_map.get(trigger) and never iterates other
                    # entries, so emit a 1-key dict rather than the full
                    # catalogue (smaller wire payload, intentional shape).
                    trigger_matches = [
                        m for m in manifests_result.data
                        if m.get("name") == trigger_service
                    ]
                    if len(trigger_matches) > 1:
                        # First-wins (matches the existing
                        # _load_manifests_from_specs dedup behaviour on the
                        # CLI side, which logs manifest_duplicate_skipped).
                        logger.warning(
                            "learn_manifest_duplicate_skipped",
                            service=trigger_service,
                            count=len(trigger_matches),
                        )
                    declared_dependencies_by_service = extract_declared_dependencies(
                        from_dicts=[trigger_matches[0]],
                    )

        # Build recommendations
        recommendations = _generate_recommendations(chain, snapshot_data)

        now = datetime.now(UTC)
        duration = snapshot_data.get("window", {}).get("duration_seconds", 0)

        data: dict[str, Any] = {
            "correlation_snapshot_id": snapshot.get("id"),
            # "quiet" or "max_age". A max_age retrospective may describe an
            # incident that is still open.
            "closed_by": closed_by,
            "duration_minutes": duration / 60 if duration else 0,
            "decisions_affected": sum(1 for v in chain if v.get("type") == "quality_breach"),
            "verdict_count": len(chain),
            "root_cause": root_cause,
            "blast_radius": snapshot_data.get("affected_services", []),
            "timeline": timeline[:20],
            "recommendations": recommendations,
            "outcome_coverage": {
                "resolved": resolved_count,
                "pending": pending_count,
                "total": len(chain),
            },
        }
        if trigger_service is not None:
            data["trigger_service"] = trigger_service
        if declared_dependencies_by_service is not None:
            data["declared_dependencies_by_service"] = declared_dependencies_by_service

        assessment = {
            # One id per snapshot, so core refuses a second retrospective
            # for it (409): "exactly one" holds whatever this process
            # remembers or can still read back.
            "id": retrospective_id(snapshot),
            "created_at": now.isoformat(),
            "kind": "retrospective",
            "service": service,
            "data": data,
        }

        result = await self.client.submit_assessment(wrap_assessment(assessment, component="learn"))
        if result.status_code == _HTTP_CONFLICT:
            # Core already holds this snapshot's retrospective, written
            # before a restart. Done, and nothing was written twice.
            logger.info("retrospective_already_written", snapshot_id=snapshot.get("id"))
            return True
        if not result.ok:
            logger.warning("retrospective_submit_failed", service=service, error=result.error)
            return False
        logger.info(
            "retrospective_emitted", service=service, verdict_count=len(chain), closed_by=closed_by
        )
        return True

    async def get_state(self) -> dict:
        state: dict[str, Any] = {}
        if self._cursor:
            state["cursor"] = self._cursor
        if self._pending:
            state["pending"] = dict(self._pending)
        return state


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _build_chain_timeline(chain: list[dict]) -> list[dict]:
    """Build a chronological timeline from a verdict chain."""
    entries = []
    for v in sorted(chain, key=lambda x: x.get("created_at", "")):
        entries.append({
            "timestamp": v.get("created_at"),
            "type": v.get("type", "unknown"),
            "service": v.get("service", "unknown"),
            "id": v.get("id"),
            "outcome": _raw_outcome_status(v),
        })
    return entries


def _generate_recommendations(chain: list[dict], snapshot_data: dict) -> list[dict]:
    """Generate recommendations from a verdict chain and snapshot data."""
    recommendations: list[dict] = []

    # SLO gate recommendation if quality_breach found
    breaches = [v for v in chain if v.get("type") == "quality_breach"]
    if breaches:
        recommendations.append({
            "type": "slo_gate",
            "detail": "Block model deploys when judgment SLO is breached",
        })

    # Dependency review if blast radius is large
    blast = snapshot_data.get("affected_services", [])
    if len(blast) > 3:
        recommendations.append({
            "type": "dependency_review",
            "detail": f"Blast radius of {len(blast)} services suggests tight coupling",
        })

    # Change control if multiple verdicts in short time
    if len(chain) > 5:
        recommendations.append({
            "type": "change_control",
            "detail": "High verdict volume suggests insufficient change control",
        })

    return recommendations
