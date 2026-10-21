"""The judgment-threshold phase of the measure cycle (opensrm-g32d)."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from nthlayer_common.cloudevents import wrap_assessment, wrap_verdict
from nthlayer_common.manifest import (
    JudgmentThreshold,
    Missing,
    OpenSRMV2ParseError,
    ThresholdOutcome,
    evaluate_threshold,
    threshold_from_v2_entry,
    threshold_to_v2_entry,
)

from nthlayer_workers.measure.thresholds.sampler import fetch_retention, needs_retention, sample
from nthlayer_workers.measure.thresholds.state import (
    REARM_INTERVALS,
    KeyState,
    advance,
    as_timestamp,
    bounds_digest,
    threshold_key,
)

logger = structlog.get_logger()

THRESHOLD_BREACH_SEVERITY = "high"
# Defaults for MeasureModule's MEASURE_THRESHOLD_INTERVAL / _HEARTBEAT.
DEFAULT_THRESHOLD_INTERVAL_SECONDS = 900.0
DEFAULT_THRESHOLD_HEARTBEAT_SECONDS = 3600.0
# MeasureModule bounds one threshold run by this (asyncio.wait_for), so a
# hanging Prometheus cannot hold the measure cycle (and its SLO phases).
THRESHOLD_PHASE_TIMEOUT_SECONDS = 120.0

_Entry = tuple[str, str, JudgmentThreshold]  # (key, service, threshold)


def _log_once(seen: set[str], token: str, event: str, **kw: Any) -> None:
    if token not in seen:
        seen.add(token)
        logger.warning(event, **kw)


def outcome_signature(outcome: ThresholdOutcome) -> str:
    """The outcome without its measured values, as a JSON string with sorted
    keys and sorted bound/segment lists: an assessment is re-emitted when
    this changes (or on the heartbeat). A bound's declared limit is part of
    it, so tightening a bound is reported even when no status changes."""
    d = outcome.to_dict()
    return json.dumps({
        "type": d["type"],
        "status": d["status"],
        "bounds": sorted(([b["bound"], b["status"], b["reason"], b["limit"]] for b in d["bounds"]),
                         key=lambda row: row[0]),
        "segments": sorted(([s["segment"], s["status"], s["reason"]] for s in d["segments"]),
                           key=lambda row: row[0]),
    }, sort_keys=True)


def _canonical(t: JudgmentThreshold) -> dict:
    """The entry's v2 form for the identical-entry check, with segment_values
    sorted: their order is not part of an entry's meaning (or its key)."""
    entry = threshold_to_v2_entry(t)
    if isinstance(entry.get("segment_values"), list):
        entry = {**entry, "segment_values": sorted(entry["segment_values"], key=str)}
    return entry


def _entries(manifests: list[dict], seen_problems: set[str]) -> tuple[set[str], list[_Entry]]:
    """Parse and key every served threshold: (services present, entries).

    "Present" means fully present, i.e. safe to retire against. A service is
    left out, so every existing key of it is kept, when any of its manifests
    has an entry that fails to parse, a judgment_thresholds that is not a
    list, or no judgment_thresholds at all (a pre-3.0 core serves none: that
    says nothing about which entries exist). An open breach must not retire
    and re-raise because a response was briefly malformed or old. Cost: a
    removed-and-broken entry lingers until the manifest parses cleanly or
    the service disappears.

    Identical entries are kept once. A threshold_key shared by several
    distinct entries of a service (same identity, other bound values) is
    suffixed for EVERY such entry with ~bounds_digest, so a key depends on
    the entry, never on its position: reordering moves no state."""
    present: set[str] = set()
    partial: set[str] = set()  # services with an entry that failed to parse
    by_base: dict[str, list[tuple[dict, str, JudgmentThreshold]]] = {}  # base -> distinct entries
    for i, m in enumerate(manifests):
        service = m.get("name") if isinstance(m, dict) else None
        if not isinstance(service, str) or not service:
            _log_once(seen_problems, f"manifest:{i}:{type(m).__name__}", "threshold_manifest_skipped",
                      where=f"/manifests[{i}]")
            continue
        items = m.get("judgment_thresholds")
        if items is None:
            # Absent (pre-3.0 core) or null: nothing to evaluate, and no
            # evidence any entry was removed, so nothing of it retires.
            partial.add(service)
            continue
        if not isinstance(items, list):
            # Partial (not just "not present"): another manifest of the same
            # name must not retire this service's keys either.
            _log_once(seen_problems, f"thresholds:{service}:{type(items).__name__}",
                      "threshold_manifest_skipped", where=f"/manifests[{i}].judgment_thresholds")
            partial.add(service)
            continue
        present.add(service)
        for j, item in enumerate(items):
            where = f"/manifests[{i}].judgment_thresholds[{j}]"
            try:
                t = threshold_from_v2_entry(item, where=where)
            except OpenSRMV2ParseError as exc:
                _log_once(seen_problems, f"parse:{service}:{item!r}", "threshold_parse_failed",
                          service=service, where=where, error=str(exc))
                partial.add(service)
                continue
            base = threshold_key(service, t)
            canonical = _canonical(t)
            same_base = by_base.setdefault(base, [])
            if any(c == canonical for c, _, _ in same_base):  # identical: evaluate once
                _log_once(seen_problems, f"dup:{base}", "threshold_duplicate", service=service, key=base)
                continue
            same_base.append((canonical, service, t))
    entries: list[_Entry] = []
    for base, distinct in by_base.items():
        if len(distinct) == 1:
            _, service, t = distinct[0]
            entries.append((base, service, t))
            continue
        used: set[str] = set()
        for _, service, t in distinct:
            key = f"{base}~{bounds_digest(t)}"
            if key in used:  # a digest collision: keep the first, never merge state
                _log_once(seen_problems, f"collision:{key}", "threshold_key_collision",
                          service=service, key=key)
                continue
            used.add(key)
            entries.append((key, service, t))
    return present - partial, entries


def _migrate(state: dict[str, dict], entries: list[_Entry]) -> dict[str, str]:
    """Carry state across a re-key; returns {old_key: new_key}.

    Adding or removing a same-identity sibling moves an entry between the
    bare key and key~bounds_digest (and editing a suffixed sibling's bound
    changes its digest). Without this the old key retires and the new one
    starts armed, so an open breach re-fires and, with autonomy on, steps
    the one-way ratchet again.

    Per service and base key, each live key with no state is matched to a
    non-live state key of the same service and base: first by equal stored
    bounds_digest (when exactly one such key has it); then, if exactly one
    old and one new key remain unmatched, the two pair (a bound edited while
    re-keyed). Anything else is ambiguous and starts fresh, the safe
    direction (it may re-raise). State without a stored base and digest
    (written before they existed) is never migrated. Runs before
    evaluation, so a migrated key is evaluated from its carried state."""
    live = {key for key, _, _ in entries}
    new: dict[tuple[str, str], list[tuple[str, str]]] = {}   # (service, base) -> [(key, digest)]
    for key, service, t in entries:
        if key not in state:
            new.setdefault((service, threshold_key(service, t)), []).append((key, bounds_digest(t)))
    old: dict[tuple[str, str], list[tuple[str, str]]] = {}   # (service, base) -> [(key, digest)]
    for key, v in state.items():
        if key in live or not isinstance(v, dict):
            continue
        service, base, digest = v.get("service"), v.get("base"), v.get("bounds_digest")
        if isinstance(service, str) and isinstance(base, str) and isinstance(digest, str):
            old.setdefault((service, base), []).append((key, digest))
    renamed: dict[str, str] = {}
    for group, new_keys in new.items():
        olds = old.get(group, [])
        if not olds:
            continue
        pairs: list[tuple[str, str, str]] = []   # (old_key, new_key, new_digest)
        for new_key, digest in new_keys:
            same = [k for k, d in olds if d == digest]
            if len(same) == 1:
                pairs.append((same[0], new_key, digest))
        matched_old = {o for o, _, _ in pairs}
        if len(matched_old) != len(pairs):   # two new keys claimed one old key
            pairs = []
            matched_old = set()
        matched_new = {n for _, n, _ in pairs}
        rest_old = [k for k, _ in olds if k not in matched_old]
        rest_new = [(k, d) for k, d in new_keys if k not in matched_new]
        if len(rest_old) == 1 and len(rest_new) == 1:
            pairs.append((rest_old[0], rest_new[0][0], rest_new[0][1]))
        for old_key, new_key, digest in pairs:
            state[new_key] = {**state.pop(old_key), "base": group[1], "bounds_digest": digest}
            renamed[old_key] = new_key
            logger.info("threshold_migrated", old_key=old_key, new_key=new_key)
    return renamed


async def _evaluate(
    *, client: Any, provider: Any, key: str, service: str, t: JudgmentThreshold,
    state: dict[str, dict], retention: float | Missing | None, retention_check: bool,
    now: float, rearm_seconds: float, heartbeat_seconds: float,
) -> tuple[str, str, str] | None:
    """Sample, evaluate, report one entry; returns its new breach, if any.
    state[key] changes only once everything this run owes is delivered."""
    outcome = evaluate_threshold(
        t, await sample(provider, service, t, now=now, retention=retention,
                        retention_check=retention_check)
    )
    prev = state.get(key) or {}
    new_state, fire = advance(KeyState.from_dict(prev), outcome.status, now, rearm_seconds)
    signature = outcome_signature(outcome)
    last_signature = prev.get("last_signature")
    last_emitted_at = as_timestamp(prev.get("last_emitted_at"))
    # A firing breach always emits: its verdict's parent_ids name this assessment.
    # A last emission in the future (the clock went back) emits too, so a
    # backward clock cannot silence the heartbeat.
    emit = (
        fire
        or signature != last_signature
        or last_emitted_at is None
        or last_emitted_at > now
        or now - last_emitted_at >= heartbeat_seconds
    )
    assessment_id = f"jte-{service}-{t.type}-{uuid.uuid4().hex[:8]}"
    if emit:
        assessment = {
            "id": assessment_id,
            "created_at": datetime.now(UTC).isoformat(),
            "kind": "judgment_threshold_evaluation",
            "service": service,
            "data": {"threshold_key": key, "outcome": outcome.to_dict()},
        }
        result = await client.submit_assessment(wrap_assessment(assessment, component="measure"))
        if result.ok:
            last_signature, last_emitted_at = signature, now
        else:
            # Not recorded as emitted, so the next run emits again. A breach
            # still raises its verdict, whose parent_ids then name an
            # assessment core did not store.
            logger.warning("threshold_assessment_submit_failed", service=service, key=key,
                           assessment_id=assessment_id, status_code=result.status_code)
    breach: tuple[str, str, str] | None = None
    if fire:
        breach_id = f"vrd-tbreach-{service}-{t.type}-{uuid.uuid4().hex[:8]}"
        verdict = {
            "id": breach_id,
            "created_at": datetime.now(UTC).isoformat(),
            "type": "quality_breach",
            "service": service,
            "parent_ids": [assessment_id],
            "data": {"threshold_key": key, "threshold_type": t.type,
                     "severity": THRESHOLD_BREACH_SEVERITY, "outcome": outcome.to_dict()},
        }
        sent = await client.submit_verdict(wrap_verdict(verdict, component="measure"))
        if not sent.ok:
            # Keep the previous state, so the next run sees the same
            # transition and raises the verdict (and its assessment) again.
            logger.warning("threshold_breach_submit_failed", service=service, key=key,
                           status_code=sent.status_code)
            return None
        breach = (key, service, breach_id)
    state[key] = {**new_state.to_dict(), "service": service,
                  "base": threshold_key(service, t), "bounds_digest": bounds_digest(t),
                  "last_signature": last_signature, "last_emitted_at": last_emitted_at}
    return breach


async def run_threshold_phase(
    *, client: Any, provider: Any, manifests: list[dict], state: dict[str, dict],
    seen_problems: set[str], eval_concurrency: int = 5, retention_check: bool = True,
    now: float | None = None,
    rearm_seconds: float = REARM_INTERVALS * DEFAULT_THRESHOLD_INTERVAL_SECONDS,
    heartbeat_seconds: float = DEFAULT_THRESHOLD_HEARTBEAT_SECONDS,
    renamed: dict[str, str] | None = None,
    breaches_out: list[tuple[str, str, str]] | None = None,
) -> list[tuple[str, str, str]]:
    """Evaluate every served threshold; mutate *state*; return new breaches as
    (key, service, breach_verdict_id). Caller guarantees *manifests* came from a
    successful /manifests response. Entries are evaluated concurrently, at most
    *eval_concurrency* at a time; one that raises is logged and skipped. An
    assessment is emitted on first sight, when the outcome's signature
    changes, after *heartbeat_seconds*, or whenever a breach fires.

    Out-parameters, filled as the run goes so a caller that cancels it (the
    worker's timeout) still sees them: *renamed* gets {old_key: new_key}
    for state carried across a re-key (before any evaluation); each breach
    whose verdict core accepted is appended to *breaches_out* at once, since
    its state has advanced and it will never fire again."""
    now = time.time() if now is None else now
    present_services, entries = _entries(manifests, seen_problems)
    moved = _migrate(state, entries)
    if renamed is not None:
        renamed.update(moved)
    # Prometheus's retention, queried once per run and only if needed.
    retention: float | Missing | None = None
    if retention_check and any(needs_retention(t) for _, _, t in entries):
        retention = await fetch_retention(provider)
    sem = asyncio.Semaphore(eval_concurrency)
    new_breaches = breaches_out if breaches_out is not None else []

    async def one(key: str, service: str, t: JudgmentThreshold) -> None:
        async with sem:
            try:
                breach = await _evaluate(client=client, provider=provider, key=key, service=service,
                                         t=t, state=state, retention=retention,
                                         retention_check=retention_check, now=now,
                                         rearm_seconds=rearm_seconds,
                                         heartbeat_seconds=heartbeat_seconds)
            except Exception as exc:  # one bad threshold affects only itself
                _log_once(seen_problems, f"eval:{key}:{type(exc).__name__}", "threshold_eval_failed",
                          service=service, key=key, error=f"{type(exc).__name__}: {exc}")
                return
            # No await between the accepted verdict and this append, so a
            # cancellation cannot drop an accepted breach.
            if breach is not None:
                new_breaches.append(breach)

    await asyncio.gather(*(one(*e) for e in entries))
    _retire(state, present_services, {key for key, _, _ in entries})
    return list(new_breaches)


def _retire(state: dict[str, dict], present_services: set[str], live_keys: set[str]) -> None:
    """Retire only keys of services present in a successful response whose
    entry is gone. An entry that is not a dict, or has no str service
    (corrupt state), can never match a manifest: drop it rather than let it
    fail every run."""
    for key in [k for k, v in state.items()
                if not isinstance(v, dict) or not isinstance(v.get("service"), str)
                or (v["service"] in present_services and k not in live_keys)]:
        del state[key]
        logger.info("threshold_retired", key=key)


def retire_thresholds(
    *, manifests: list[dict], state: dict[str, dict], seen_problems: set[str],
    renamed: dict[str, str] | None = None,
) -> None:
    """Garbage collection alone, for measure cycles on which the threshold
    phase does not run: /manifests is fetched every cycle, so a removed
    entry retires (and leaves governance) without waiting for the next
    threshold run. Same rule as the phase: successful response, service
    present, entry gone; and the same re-key migration first (*renamed*
    gets {old_key: new_key}). Parses only; sends no query."""
    present_services, entries = _entries(manifests, seen_problems)
    moved = _migrate(state, entries)
    if renamed is not None:
        renamed.update(moved)
    _retire(state, present_services, {key for key, _, _ in entries})
