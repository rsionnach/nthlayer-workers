"""Threshold state follows sibling re-keys; the phase is bounded (opensrm-g32d, R5 edge-cases).

Adding or removing a same-identity sibling (same threshold_key, other bound
values) moves an entry between the bare key and key~bounds_digest. Without
migration the old key retires and the new key starts armed, so an open
breach re-fires (and, with MEASURE_THRESHOLD_AUTONOMY on, steps the one-way
ratchet again).
"""

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest
from nthlayer_common.api_client import APIResult
from nthlayer_common.manifest import threshold_from_v2_entry

from nthlayer_workers.measure.thresholds.phase import retire_thresholds, run_threshold_phase
from nthlayer_workers.measure.thresholds.state import bounds_digest, threshold_key
from nthlayer_workers.measure.worker import MeasureModule


def _cal(limit):
    return {"type": "calibration", "measurement": {"bins": 10, "window": "30d"},
            "bounds": {"maximum_expected_calibration_error": limit}}


# Measured ECE is 0.2 throughout: A (0.05) breaches, B (0.5) is within.
A, B = _cal(0.05), _cal(0.5)
A_EDITED = _cal(0.06)   # still breaching, other digest
B_EDITED = _cal(0.6)    # still within, other digest
NOW = 1_800_000_000.0


def _t(entry):
    return threshold_from_v2_entry(entry)


BASE = threshold_key("svc", _t(A))  # every calibration entry above shares it


def _sib(entry):
    return f"{BASE}~{bounds_digest(_t(entry))}"


class _Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self):
        return self.t


def _module(monkeypatch, autonomy=True):
    monkeypatch.setenv("MEASURE_THRESHOLD_AUTONOMY", "on" if autonomy else "off")
    client = AsyncMock()
    client.submit_assessment = AsyncMock(return_value=APIResult(ok=True, status_code=201, data={}))
    verdicts = []
    client.submit_verdict = AsyncMock(side_effect=lambda v: (verdicts.append(v["data"]),
                                      APIResult(ok=True, status_code=201, data={}))[1])
    prov = AsyncMock()
    prov.query = AsyncMock(return_value={"status": "success",
                                         "data": {"result": [{"metric": {}, "value": [0, "0.2"]}]}})
    prov.aclose = AsyncMock()
    m = MeasureModule(client=client, prometheus_url="http://p", clock=_Clock(),
                      threshold_interval_seconds=900.0)
    return m, prov, verdicts


async def _cycle(m, prov, entries, *, at):
    m.clock.t = NOW + at
    m.client.get_manifests = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=[
        {"name": "svc", "type": "ai-gate", "slos": [], "judgment_thresholds": entries}]))
    with patch("nthlayer_workers.measure.worker.PrometheusProvider", return_value=prov):
        await m.process_cycle()


def _types(verdicts):
    return [v["type"] for v in verdicts]


def _open_breach(m, key):
    st = m._threshold_state[key]
    return st["last_evaluable"] == "breach" and st["armed"] is False


def _governed(m, key):
    return key in m._threshold_governed and m._breach_decisions.get(key, {}).get("decided")


# --- Migration across a split and a merge (threshold runs) ------------------

async def test_adding_then_removing_a_sibling_carries_the_open_breach(monkeypatch):
    m, prov, v = _module(monkeypatch)
    await _cycle(m, prov, [A], at=0)
    assert _types(v) == ["quality_breach", "autonomy_change"]
    assert m._threshold_state[BASE]["base"] == BASE
    assert m._threshold_state[BASE]["bounds_digest"] == bounds_digest(_t(A))

    await _cycle(m, prov, [A, B], at=900)       # split: A -> BASE~dA, B new
    assert set(m._threshold_state) == {_sib(A), _sib(B)}
    assert _open_breach(m, _sib(A)) and _governed(m, _sib(A))
    assert BASE not in m._breach_decisions and BASE not in m._threshold_governed
    assert m._threshold_state[_sib(B)]["armed"] is True       # B started fresh
    assert m._threshold_state[_sib(A)]["bounds_digest"] == bounds_digest(_t(A))

    await _cycle(m, prov, [A], at=1800)         # merge: BASE~dA -> BASE, B retired
    assert set(m._threshold_state) == {BASE}
    assert _open_breach(m, BASE) and _governed(m, BASE)
    # No second quality_breach, no second autonomy step.
    assert _types(v) == ["quality_breach", "autonomy_change"]
    assert m._autonomy_levels["svc"] != "fully_autonomous"


async def test_an_edited_sibling_bound_pairs_its_state(monkeypatch):
    """Editing a suffixed sibling's bound re-keys it (the digest is in the
    key): one unmatched old key and one unmatched new key of the base pair."""
    m, prov, v = _module(monkeypatch)
    await _cycle(m, prov, [A, B], at=0)
    assert _types(v) == ["quality_breach", "autonomy_change"]
    await _cycle(m, prov, [A_EDITED, B], at=900)
    assert set(m._threshold_state) == {_sib(A_EDITED), _sib(B)}
    assert _open_breach(m, _sib(A_EDITED)) and _governed(m, _sib(A_EDITED))
    assert m._threshold_state[_sib(A_EDITED)]["bounds_digest"] == bounds_digest(_t(A_EDITED))
    assert _types(v) == ["quality_breach", "autonomy_change"]


async def test_digest_match_and_pairing_via_retire_thresholds(monkeypatch):
    """Split and edit in one run: A unchanged is matched by digest; the one
    remaining old key (B) pairs with the one remaining new key (B edited)."""
    state = {
        _sib(A): {"last_evaluable": "breach", "armed": False, "within_since": None,
                  "within_count": 0, "service": "svc", "base": BASE,
                  "bounds_digest": bounds_digest(_t(A))},
        _sib(B): {"last_evaluable": "within", "armed": True, "within_since": NOW,
                  "within_count": 1, "service": "svc", "base": BASE,
                  "bounds_digest": bounds_digest(_t(B))},
    }
    marker = state[_sib(B)]
    renamed: dict[str, str] = {}
    retire_thresholds(manifests=[{"name": "svc", "judgment_thresholds": [A, B_EDITED, _cal(0.9)]}],
                      state=state, seen_problems=set(), renamed=renamed)
    # A's key is unchanged (still suffixed), so nothing to migrate for it; B's
    # old key vs two new keys (B_EDITED and 0.9) is ambiguous: fresh.
    assert renamed == {}
    assert _sib(B) not in state and marker["within_count"] == 1

    state = {
        BASE: {"last_evaluable": "breach", "armed": False, "within_since": None,
               "within_count": 0, "service": "svc", "base": BASE,
               "bounds_digest": bounds_digest(_t(A))},
        _sib(B): {"last_evaluable": "within", "armed": True, "within_since": NOW,
                  "within_count": 2, "service": "svc", "base": BASE,
                  "bounds_digest": bounds_digest(_t(B))},
    }
    renamed = {}
    retire_thresholds(manifests=[{"name": "svc", "judgment_thresholds": [A, B_EDITED]}],
                      state=state, seen_problems=set(), renamed=renamed)
    assert renamed == {BASE: _sib(A), _sib(B): _sib(B_EDITED)}
    assert state[_sib(A)]["last_evaluable"] == "breach"
    assert state[_sib(B_EDITED)]["within_count"] == 2
    assert state[_sib(B_EDITED)]["bounds_digest"] == bounds_digest(_t(B_EDITED))


async def test_ambiguous_rekey_starts_fresh(monkeypatch):
    """Both siblings edited at once: two unmatched old keys, two unmatched
    new keys, no digest match. No guessing: fresh state (the safe direction,
    which re-raises)."""
    m, prov, v = _module(monkeypatch)
    await _cycle(m, prov, [A, B], at=0)
    await _cycle(m, prov, [A_EDITED, B_EDITED], at=900)
    assert set(m._threshold_state) == {_sib(A_EDITED), _sib(B_EDITED)}
    assert _types(v).count("quality_breach") == 2


def test_state_without_base_or_digest_is_never_migrated():
    legacy = {"last_evaluable": "breach", "armed": False, "within_since": None,
              "within_count": 0, "service": "svc"}
    state = {BASE: dict(legacy)}
    renamed: dict[str, str] = {}
    retire_thresholds(manifests=[{"name": "svc", "judgment_thresholds": [A, B]}],
                      state=state, seen_problems=set(), renamed=renamed)
    assert renamed == {} and BASE not in state


# --- Migration on a not-run cycle (retire_thresholds path) ------------------

async def test_split_on_a_non_threshold_cycle_migrates_state_and_governance(monkeypatch):
    m, prov, v = _module(monkeypatch)
    await _cycle(m, prov, [A], at=0)
    queries = prov.query.await_count
    await _cycle(m, prov, [A, B], at=60)        # inside the interval: no threshold run
    assert prov.query.await_count == queries
    assert set(m._threshold_state) == {_sib(A)}
    assert _open_breach(m, _sib(A)) and _governed(m, _sib(A))
    assert m._breach_severities.get(_sib(A)) == "high" and BASE not in m._breach_severities
    await _cycle(m, prov, [A, B], at=900)       # threshold run: B evaluated fresh
    await _cycle(m, prov, [A], at=960)          # merge on a non-threshold cycle
    assert set(m._threshold_state) == {BASE}
    assert _open_breach(m, BASE) and _governed(m, BASE)
    assert _types(v) == ["quality_breach", "autonomy_change"]


async def test_run_threshold_phase_reports_renames():
    state = {BASE: {"last_evaluable": "breach", "armed": False, "within_since": None,
                    "within_count": 0, "service": "svc", "base": BASE,
                    "bounds_digest": bounds_digest(_t(A))}}
    prov = AsyncMock()
    prov.query = AsyncMock(return_value={"status": "success",
                                         "data": {"result": [{"metric": {}, "value": [0, "0.2"]}]}})
    client = AsyncMock()
    client.submit_assessment = AsyncMock(return_value=APIResult(ok=True, status_code=201, data={}))
    client.submit_verdict = AsyncMock(return_value=APIResult(ok=True, status_code=201, data={}))
    renamed: dict[str, str] = {}
    breaches = await run_threshold_phase(
        client=client, provider=prov, state=state, seen_problems=set(), now=NOW, renamed=renamed,
        manifests=[{"name": "svc", "judgment_thresholds": [A, B]}])
    assert renamed == {BASE: _sib(A)}
    assert breaches == [] and client.submit_verdict.await_count == 0


# --- MINORs: absent key, duplicate service names -----------------------------

_KEPT = {"last_evaluable": "breach", "armed": False, "within_since": None,
         "within_count": 0, "service": "svc"}


@pytest.mark.parametrize("manifests", [
    [{"name": "svc", "type": "ai-gate", "slos": []}],                              # pre-3.0: no key
    [{"name": "svc", "judgment_thresholds": []}, {"name": "svc", "judgment_thresholds": 5}],
    [{"name": "svc", "judgment_thresholds": 5}, {"name": "svc", "judgment_thresholds": []}],
    [{"name": "svc", "judgment_thresholds": []}, {"name": "svc"}],
])
async def test_service_not_fully_served_keeps_its_state(manifests):
    state = {BASE: dict(_KEPT)}
    retire_thresholds(manifests=manifests, state=state, seen_problems=set())
    assert state == {BASE: _KEPT}
    state = {BASE: dict(_KEPT)}
    await run_threshold_phase(client=AsyncMock(), provider=AsyncMock(), manifests=manifests,
                              state=state, seen_problems=set(), now=NOW)
    assert state == {BASE: _KEPT}


# --- Bounded phase -----------------------------------------------------------

_RR_SLO = {"name": "rr", "target": 98.5, "judgment_type": "reversal_rate", "window": "30d",
           "indicator_query": "q"}


async def test_a_hanging_provider_cannot_hold_the_cycle(monkeypatch):
    """s1's breach is accepted before s2's query hangs: the timeout must not
    lose it (state advanced, so it would never fire again), and Phases 2-3
    still run."""
    m, _, v = _module(monkeypatch)
    m.client.get_manifests = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=[
        {"name": "s1", "type": "ai-gate", "slos": [_RR_SLO], "judgment_thresholds": [A]},
        {"name": "s2", "type": "ai-gate", "slos": [], "judgment_thresholds": [A]},
    ]))

    async def query(q, time=None):
        if 's2' in q:
            await asyncio.Event().wait()   # hangs forever
        return {"status": "success", "data": {"result": [{"metric": {}, "value": [0, "0.2"]}]}}

    prov = AsyncMock()
    prov.query = AsyncMock(side_effect=query)
    prov.get_sli_value = AsyncMock(return_value=0.98)   # SLO rr breaches
    prov.aclose = AsyncMock()
    s1_key = threshold_key("s1", _t(A))
    started = time.monotonic()
    with patch("nthlayer_workers.measure.worker.THRESHOLD_PHASE_TIMEOUT_SECONDS", 0.2), \
         patch("nthlayer_workers.measure.worker.PrometheusProvider", return_value=prov):
        await asyncio.wait_for(m.process_cycle(), 5)
    assert time.monotonic() - started < 2
    breaches = [x for x in v if x["type"] == "quality_breach"]
    assert {x["data"].get("slo_name") for x in breaches if "slo_name" in x["data"]} == {"rr"}
    assert [x["data"]["threshold_key"] for x in breaches if "threshold_key" in x["data"]] == [s1_key]
    # The accepted threshold breach entered governance.
    assert _governed(m, s1_key)
    autonomy = [x for x in v if x["type"] == "autonomy_change"]
    assert len(autonomy) == 1 and autonomy[0]["data"]["breaching_slo_count"] == 2


# --- Stored identity refreshed every cycle (R5 edge-cases re-check) ---------

async def test_bound_edited_then_sibling_added_on_non_run_cycles_keeps_the_breach(monkeypatch):
    """probe2: A's bound is edited on a non-run cycle (key unchanged, so the
    stored digest would go stale), then a sibling is added on another: the
    split must still digest-match A's state."""
    m, prov, v = _module(monkeypatch)
    await _cycle(m, prov, [A], at=0)
    await _cycle(m, prov, [A_EDITED], at=60)              # same bare key, new digest
    assert m._threshold_state[BASE]["bounds_digest"] == bounds_digest(_t(A_EDITED))
    await _cycle(m, prov, [A_EDITED, B], at=120)          # split, non-run
    assert _open_breach(m, _sib(A_EDITED)) and _governed(m, _sib(A_EDITED))
    await _cycle(m, prov, [A_EDITED, B], at=900)          # threshold run
    assert _types(v) == ["quality_breach", "autonomy_change"]


async def test_restored_state_without_identity_is_refreshed_then_migrates(monkeypatch):
    m, prov, v = _module(monkeypatch)
    await _cycle(m, prov, [A], at=0)
    saved = await m.get_state()
    for st in saved["threshold_state"].values():          # state from before the fix
        del st["base"], st["bounds_digest"]
    m2, prov2, v2 = _module(monkeypatch)
    await m2.restore_state(saved)
    await _cycle(m2, prov2, [A], at=60)                   # non-run: refreshes identity
    await _cycle(m2, prov2, [A, B], at=120)               # split, non-run
    assert _open_breach(m2, _sib(A)) and _governed(m2, _sib(A))
    await _cycle(m2, prov2, [A, B], at=900)
    assert v2 == []                                       # nothing re-raised


# --- Rotating evaluation start (tail starvation) ----------------------------

async def test_rotation_reaches_entries_behind_a_hanging_one(monkeypatch):
    m, _, v = _module(monkeypatch, autonomy=False)
    m.threshold_interval_seconds = 1.0
    m._eval_concurrency = 1
    m.client.get_manifests = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=[
        {"name": f"s{i}", "type": "ai-gate", "slos": [], "judgment_thresholds": [A]} for i in range(3)]))
    reached: list[set[str]] = []
    m.client.submit_assessment = AsyncMock(side_effect=lambda env: (
        reached[-1].add(env["data"]["service"]), APIResult(ok=True, status_code=201, data={}))[1])

    async def query(q, time=None):
        if 's1' in q:
            await asyncio.Event().wait()   # s1 hangs every run
        return {"status": "success", "data": {"result": [{"metric": {}, "value": [0, "0.01"]}]}}

    prov = AsyncMock()
    prov.query = AsyncMock(side_effect=query)
    prov.aclose = AsyncMock()
    with patch("nthlayer_workers.measure.worker.THRESHOLD_PHASE_TIMEOUT_SECONDS", 0.1), \
         patch("nthlayer_workers.measure.worker.PrometheusProvider", return_value=prov):
        for i in range(3):
            reached.append(set())
            m.clock.t = NOW + i
            await asyncio.wait_for(m.process_cycle(), 5)
    assert len({frozenset(r) for r in reached}) > 1          # runs reached different entries
    assert set().union(*reached) == {"s0", "s2"}              # s2 is not starved
    assert set(m._threshold_state) == {threshold_key(f"s{i}", _t(A)) for i in (0, 2)}


@pytest.mark.parametrize("rotation", [0, 1, 2, 7])
async def test_rotation_never_changes_keys(rotation):
    client = AsyncMock()
    client.submit_assessment = AsyncMock(return_value=APIResult(ok=True, status_code=201, data={}))
    client.submit_verdict = AsyncMock(return_value=APIResult(ok=True, status_code=201, data={}))
    prov = AsyncMock()
    prov.query = AsyncMock(return_value={"status": "success",
                                         "data": {"result": [{"metric": {}, "value": [0, "0.2"]}]}})
    state: dict[str, dict] = {}
    await run_threshold_phase(client=client, provider=prov, state=state, seen_problems=set(),
                              now=NOW, rotation=rotation,
                              manifests=[{"name": "svc", "judgment_thresholds": [A, B]}])
    assert set(state) == {_sib(A), _sib(B)}
    assert state[_sib(A)]["last_evaluable"] == "breach" and state[_sib(B)]["last_evaluable"] == "within"


async def test_unreached_holds_the_keys_never_started_after_a_cancel():
    client = AsyncMock()
    client.submit_assessment = AsyncMock(return_value=APIResult(ok=True, status_code=201, data={}))

    async def query(q, time=None):
        if 's0' in q:
            await asyncio.Event().wait()
        return {"status": "success", "data": {"result": [{"metric": {}, "value": [0, "0.01"]}]}}

    prov = AsyncMock()
    prov.query = AsyncMock(side_effect=query)
    unreached: set[str] = set()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(run_threshold_phase(
            client=client, provider=prov, state={}, seen_problems=set(), now=NOW,
            eval_concurrency=1, unreached=unreached,
            manifests=[{"name": f"s{i}", "judgment_thresholds": [A]} for i in range(3)]), 0.1)
    assert unreached == {threshold_key(f"s{i}", _t(A)) for i in (1, 2)}
