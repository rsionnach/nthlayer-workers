"""Learn worker modules — outcome resolution and retrospective generation.

Two modules implementing WorkerModule protocol:
- LearnOutcomeModule: continuous verdict outcome resolution + calibration signals (60s)
- LearnRetrospectiveModule: incident retrospective triggered by correlation_snapshot (30s)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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


@dataclass
class LearnOutcomeModule:
    """Continuous background maintenance of verdict outcome state.

    Each process_cycle():
    1. Fetches pending verdicts older than minimum_resolution_age
    2. Attempts resolution via five paths (lineage, calibration sampling,
       downstream signal, score-outcome divergence, expiry)
    3. Emits calibration_signal assessment when outcomes resolve
    4. Marks expired verdicts past threshold

    Calibration signals feed measure module's self-calibration (v2).
    Expired verdicts don't produce signals — absence of data is not
    a quality signal.
    """

    client: CoreAPIClient
    expiry_threshold_days: int = 7
    minimum_resolution_age_hours: int = 1
    _cursor: str | None = None

    @property
    def name(self) -> str:
        return "learn.outcome"

    async def restore_state(self, state: dict | None) -> None:
        if state:
            self._cursor = state.get("cursor")

    async def process_cycle(self) -> None:
        now = datetime.now(UTC)

        # Minimum age floor: skip verdicts younger than threshold because
        # downstream signals may not have arrived yet. Premature resolution
        # attempts always return "no signal found" and waste cycles.
        age_cutoff = now - timedelta(hours=self.minimum_resolution_age_hours)

        result = await self.client.get_verdicts(
            created_before=age_cutoff.isoformat(),
            created_after=self._cursor,
            limit=50,
        )
        if not result.ok or not result.data:
            return

        for verdict in result.data:
            if verdict.get("type") in _RESOLUTION_RECORD_TYPES:
                continue
            if _outcome_status(verdict) != _PENDING:
                continue

            try:
                # Attempt resolution via five paths
                resolution = await self._attempt_resolution(verdict)
                if resolution:
                    submit = await self.client.resolve_outcome(verdict["id"], resolution)
                    if submit.ok:
                        await self._emit_calibration_signal(verdict, resolution)
                    continue

                # Expiry fallback
                if self._is_past_expiry(verdict, now):
                    await self.client.resolve_outcome(verdict["id"], {
                        "outcome_status": "expired",
                        "resolution": "No outcome signal within threshold",
                    })
            except Exception:
                logger.warning("outcome_resolution_failed", verdict_id=verdict.get("id"))

            # Advance cursor past this verdict regardless of outcome
            created = verdict.get("created_at", "")
            if created and (not self._cursor or created > self._cursor):
                self._cursor = created

    async def _attempt_resolution(self, verdict: dict) -> dict | None:
        """Try the five resolution paths in order. Returns resolution dict or None."""
        verdict_id = verdict.get("id", "")

        # Paths 1+4: Lineage + divergence — single pass over descendants.
        # Check for downstream verdicts that resolve this one.
        # Priority: overridden > confirmed (an execution that was overridden
        # is a divergence signal, not a confirmation).
        descendants = await self.client.get_descendants(verdict_id)
        if descendants.ok and descendants.data:
            has_execution = None
            has_overridden = None
            for desc in descendants.data:
                desc_type = desc.get("type", "")
                if _outcome_status(desc) == "overridden" and has_overridden is None:
                    has_overridden = desc
                if has_execution is None and (
                    desc_type == "execution"
                    or _resolves(desc, verdict_id)
                ):
                    has_execution = desc

            # Overridden takes priority (Path 4: divergence)
            if has_overridden:
                return {
                    "outcome_status": "overridden",
                    "resolution": f"Overridden by {has_overridden.get('id')}",
                    "path": "divergence",
                }
            # Execution/outcome_resolution confirms (Path 1: lineage)
            if has_execution:
                return {
                    "outcome_status": "confirmed",
                    "resolution": f"Resolved by downstream {has_execution.get('type')} verdict {has_execution.get('id')}",
                    "path": "lineage",
                }

        # Path 2: Calibration sampling — v1.5 stub (infrastructure not yet built)
        # Path 3: Downstream signal — captured by lineage when external signals
        #   create verdicts with parent_ids pointing to this verdict.
        # Path 5: Expiry — handled in the caller's expiry fallback
        return None

    def _is_past_expiry(self, verdict: dict, now: datetime) -> bool:
        """Check if a verdict has exceeded the expiry threshold."""
        if self.expiry_threshold_days <= 0:
            return False  # 0 or negative threshold disables expiry
        created = verdict.get("created_at", "")
        if not created:
            return False
        try:
            created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            return (now - created_dt) >= timedelta(days=self.expiry_threshold_days)
        except (ValueError, AttributeError):
            return False

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
            state["cursor"] = self._cursor
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

# Core's answer to an assessment whose id it already holds.
_HTTP_CONFLICT = 409


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
        # it. Its own ``outcome`` field is not read: core never updates it.
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
