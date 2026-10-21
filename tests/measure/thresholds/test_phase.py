"""Threshold phase of MeasureModule (opensrm-g32d)."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import structlog
import yaml
from nthlayer_common.api_client import APIResult
from nthlayer_common.manifest import (
    AtLeast,
    Missing,
    Samples,
    evaluate_threshold,
    threshold_from_v2_entry,
    threshold_to_v2_entry,
)

from nthlayer_workers.measure.thresholds.phase import breach_readout, outcome_signature
from nthlayer_workers.measure.thresholds.state import bounds_digest, threshold_key
from nthlayer_workers.measure.worker import MeasureModule

_CAL = {"type": "calibration", "measurement": {"bins": 10, "window": "30d"},
        "bounds": {"maximum_expected_calibration_error": 0.05}}


def _manifests(*services):
    return APIResult(ok=True, status_code=200, data=[
        {"name": name, "type": "ai-gate", "slos": [], "judgment_thresholds": entries}
        for name, entries in services
    ])


def _vector(v):
    return {"status": "success", "data": {"result": [{"metric": {}, "value": [0, str(v)]}]}}


NOW = 1_800_000_000.0


class _Clock:
    """A settable clock for MeasureModule.clock: advance with `m.clock.t += s`."""

    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


def _module(prom_values, manifests, **module_kw):
    """prom_values: list of ECE values returned on successive queries.
    Defaults: a 1s threshold interval, and _cycles advances the clock by the
    interval after each cycle, so every cycle is a threshold run. A test that
    needs exact times sets m.clock.t itself before each cycle."""
    module_kw.setdefault("threshold_interval_seconds", 1.0)
    module_kw.setdefault("clock", _Clock())
    client = AsyncMock()
    client.get_manifests = AsyncMock(side_effect=manifests if isinstance(manifests, list) else None,
                                     return_value=None if isinstance(manifests, list) else manifests)
    assessments, verdicts = [], []
    client.submit_assessment = AsyncMock(side_effect=lambda a: (assessments.append(a["data"]),
                                         APIResult(ok=True, status_code=201, data={}))[1])
    client.submit_verdict = AsyncMock(side_effect=lambda v: (verdicts.append(v["data"]),
                                      APIResult(ok=True, status_code=201, data={}))[1])
    prov = AsyncMock()
    prov.query = AsyncMock(side_effect=[_vector(v) for v in prom_values])
    prov.aclose = AsyncMock()
    module = MeasureModule(client=client, prometheus_url="http://prom:9090", **module_kw)
    return module, prov, assessments, verdicts


def _breaches(verdicts):
    return [v for v in verdicts if v.get("type") == "quality_breach"]


async def _cycles(module, prov, n):
    with patch("nthlayer_workers.measure.worker.PrometheusProvider", return_value=prov):
        for _ in range(n):
            await module.process_cycle()
            module.clock.t += module.threshold_interval_seconds


async def test_breach_emits_assessment_and_one_high_verdict():
    m, prov, a, v = _module([0.2, 0.2], _manifests(("svc", [_CAL])))
    await _cycles(m, prov, 2)
    evals = [x for x in a if x["kind"] == "judgment_threshold_evaluation"]
    # The second, unchanged outcome inside the heartbeat is not re-emitted.
    assert len(evals) == 1 and evals[0]["data"]["outcome"]["status"] == "breach"
    assert len(_breaches(v)) == 1
    assert _breaches(v)[0]["data"]["severity"] == "high"
    assert _breaches(v)[0]["parent_ids"] == [evals[0]["id"]]


async def test_high_ece_breaches_only_the_ece_bound():
    entry = dict(_CAL, bounds={"maximum_expected_calibration_error": 0.05, "maximum_brier_score": 0.5})
    m, prov, a, v = _module([0.2, 0.01], _manifests(("svc", [entry])))  # ECE then Brier
    await _cycles(m, prov, 1)
    bounds = {b["bound"]: b["status"] for b in a[0]["data"]["outcome"]["bounds"]}
    assert bounds == {"maximum_expected_calibration_error": "breach", "maximum_brier_score": "within"}


async def test_not_evaluable_is_reported_but_never_breaches():
    entry = {"type": "audit_backlog", "bounds": {"maximum_age": "24h"}}
    m, prov, a, v = _module([], _manifests(("svc", [entry])))
    await _cycles(m, prov, 1)
    assert a[0]["data"]["outcome"]["status"] == "not_evaluable"
    assert a[0]["data"]["outcome"]["bounds"][0]["reason"] == "no_data_source"
    assert _breaches(v) == []


_CAL_02 = dict(_CAL, bounds={"maximum_expected_calibration_error": 0.2})


def _suffixed(entry):
    t = threshold_from_v2_entry(entry)
    return f"{threshold_key('svc', t)}~{bounds_digest(t)}"


async def test_same_identity_entries_get_value_suffixed_keys():
    m, prov, a, v = _module([0.1, 0.1], _manifests(("svc", [_CAL, _CAL_02])))
    await _cycles(m, prov, 1)
    by_key = {x["data"]["threshold_key"]: x["data"]["outcome"]["status"] for x in a}
    # EVERY entry sharing the identity is suffixed, not just the later ones.
    assert by_key == {_suffixed(_CAL): "breach", _suffixed(_CAL_02): "within"}


async def test_reordering_same_identity_entries_keeps_each_entrys_state():
    m, prov, a, v = _module([0.1, 0.1, 0.1, 0.1], [
        _manifests(("svc", [_CAL, _CAL_02])),
        _manifests(("svc", [_CAL_02, _CAL])),  # same entries, swapped
    ])
    await _cycles(m, prov, 1)
    before = {k: dict(st) for k, st in m._threshold_state.items()}
    await _cycles(m, prov, 1)
    assert set(m._threshold_state) == set(before) == {_suffixed(_CAL), _suffixed(_CAL_02)}
    assert m._threshold_state[_suffixed(_CAL)]["last_evaluable"] == "breach"   # 0.1 > 0.05
    assert m._threshold_state[_suffixed(_CAL_02)]["last_evaluable"] == "within"  # 0.1 <= 0.2
    assert len(_breaches(v)) == 1  # the swap re-raised nothing


async def test_a_lone_entry_keeps_the_bare_key():
    m, prov, a, v = _module([0.1], _manifests(("svc", [_CAL])))
    await _cycles(m, prov, 1)
    assert set(m._threshold_state) == {threshold_key("svc", threshold_from_v2_entry(_CAL))}


async def test_duplicate_entries_emit_once():
    m, prov, a, v = _module([0.2], _manifests(("svc", [_CAL, dict(_CAL)])))
    await _cycles(m, prov, 1)
    assert len([x for x in a if x["kind"] == "judgment_threshold_evaluation"]) == 1


async def test_unparseable_entry_is_isolated():
    m, prov, a, v = _module([0.2], _manifests(("svc", [{"type": "segments"}, _CAL])))
    await _cycles(m, prov, 1)
    assert len(a) == 1 and a[0]["data"]["outcome"]["type"] == "calibration"


async def test_core_unreachable_touches_nothing():
    m, prov, a, v = _module([0.2], APIResult(ok=False, status_code=503, data=None))
    m._threshold_state = {"svc:calibration:abc": {"last_evaluable": "breach", "armed": False, "within_since": None,
                                                 "service": "svc"}}
    await _cycles(m, prov, 1)
    assert a == [] and v == []
    assert "svc:calibration:abc" in m._threshold_state


async def test_absent_service_keeps_state_and_removed_entry_retires():
    m, prov, a, v = _module([0.2, 0.2], [
        _manifests(("svc", [_CAL]), ("other", [])),
        _manifests(("other", [])),           # svc absent -> keep its state
    ])
    await _cycles(m, prov, 2)
    assert any(k.startswith("svc:") for k in m._threshold_state)
    m2, prov2, *_ = _module([0.2], [_manifests(("svc", [_CAL])), _manifests(("svc", []))])
    await _cycles(m2, prov2, 2)
    assert not any(k.startswith("svc:") for k in m2._threshold_state)


async def test_service_without_thresholds_key_is_ignored():
    data = APIResult(ok=True, status_code=200, data=[{"name": "old", "type": "ai-gate", "slos": []}])
    m, prov, a, v = _module([], data)
    await _cycles(m, prov, 1)
    assert a == [] and v == []


async def test_restart_does_not_refire():
    m, prov, a, v = _module([0.2], _manifests(("svc", [_CAL])))
    await _cycles(m, prov, 1)
    saved = await m.get_state()
    m2, prov2, a2, v2 = _module([0.2], _manifests(("svc", [_CAL])))
    await m2.restore_state(saved)
    m2.clock.t = m.clock.t           # one interval after the saved run
    await _cycles(m2, prov2, 1)
    assert prov2.query.await_count == 1  # the phase ran...
    assert _breaches(v2) == []           # ...and did not re-raise


async def test_saved_state_carries_threshold_state_version_1():
    m, prov, a, v = _module([0.2], _manifests(("svc", [_CAL])))
    await _cycles(m, prov, 1)
    assert (await m.get_state())["threshold_state_version"] == 1


@pytest.mark.parametrize("version", ["absent", 1])
async def test_version_1_or_no_version_restores_threshold_state(version):
    # No marker: state saved before the marker existed, which is version 1.
    m, prov, a, v = _module([0.2], _manifests(("svc", [_CAL])))
    await _cycles(m, prov, 1)
    saved = await m.get_state()
    if version == "absent":
        del saved["threshold_state_version"]
    else:
        saved["threshold_state_version"] = version
    m2, prov2, a2, v2 = _module([0.2], _manifests(("svc", [_CAL])))
    await m2.restore_state(saved)
    assert m2._threshold_state == saved["threshold_state"] != {}
    m2.clock.t = m.clock.t
    await _cycles(m2, prov2, 1)
    assert prov2.query.await_count == 1 and _breaches(v2) == []


@pytest.mark.parametrize("version", [2, 0, "1", 1.0, True, None, [1]])
async def test_an_unknown_threshold_state_version_discards_only_threshold_state(version):
    key = "svc:calibration:abc"
    saved = {
        "slo_status": {"svc:rr": "breach"},
        "breach_decisions": {"svc:rr": {"decided": True}, key: {"decided": True}},
        "autonomy_levels": {"svc": "supervised"},
        "breach_severities": {"svc:rr": "high"},
        "threshold_state": {key: {"service": "svc", "last_evaluable": "breach", "armed": False}},
        "threshold_last_run": NOW,
        "threshold_governed": [key],
        "threshold_state_version": version,
    }
    m, _, _, _ = _module([], _manifests(("svc", [_CAL])))
    with structlog.testing.capture_logs() as logs:
        await m.restore_state(saved)
    assert m._threshold_state == {} and m._threshold_governed == set()
    assert [e for e in logs if e["event"] == "threshold_state_version_unknown"] == [
        {"event": "threshold_state_version_unknown", "version": version, "log_level": "warning"}]
    # Everything else is restored as saved.
    assert m._slo_status == saved["slo_status"]
    assert m._breach_decisions == saved["breach_decisions"]
    assert m._autonomy_levels == saved["autonomy_levels"]
    assert m._breach_severities == saved["breach_severities"]
    assert m._threshold_last_run == NOW


_SPEC_V2 = Path(__file__).resolve().parents[4] / "opensrm" / "spec" / "v2"


async def test_served_shape_drives_a_cycle():
    """Exactly what core 3.0 serves: threshold_to_v2_entry of the spec examples."""
    src = _SPEC_V2 / "examples" / "judgment-thresholds" / "all-five-types.yaml"
    assert src.is_file(), src
    served = [threshold_to_v2_entry(t) for t in
              (threshold_from_v2_entry(e)
               for e in yaml.safe_load(src.read_text())["spec"]["judgment_threshold"])]
    m, _, a, v = _module([], _manifests(("svc", served)))

    def answer(q):
        if "_bucket" in q:
            # 10 latencies: 5 <= 1h, 5 in (1h, 1d]. p50 rank 5 -> exactly 3600
            # (within 1h); p90 rank 9 -> 3600 + 82800 * 4/5 = 69840 (within 24h).
            return {"status": "success", "data": {"result": [
                {"metric": {"le": le}, "value": [0, n]}
                for le, n in (("3600", "5"), ("86400", "10"), ("+Inf", "10"))]}}
        return _vector(0.01)  # Brier 0.01 (bound 0.05); retention start 0.01 (decades back)

    prov = _recording_provider(answer)
    await _cycles(m, prov, 1)
    outcomes = {x["data"]["outcome"]["type"]: x["data"]["outcome"] for x in a}
    assert len(a) == len(served) and set(outcomes) == {t["type"] for t in served}
    assert {t: o["status"] for t, o in outcomes.items()} == {
        "segments": "not_evaluable", "stability": "not_evaluable", "audit_backlog": "not_evaluable",
        "calibration": "within", "feedback_latency": "within"}
    for t in ("segments", "stability", "audit_backlog"):
        assert {b["reason"] for b in outcomes[t]["bounds"]} == {"no_data_source"}, t
    assert v == []


# --- Breach verdicts readable downstream -----------------------------------

async def test_breach_verdict_names_its_breaching_bounds_and_summarises():
    entry = dict(_CAL, bounds={"maximum_expected_calibration_error": 0.05, "maximum_brier_score": 0.5})
    m, prov, a, v = _module([0.12, 0.01], _manifests(("svc", [entry])))  # ECE then Brier
    await _cycles(m, prov, 1)
    (breach,) = _breaches(v)
    data = breach["data"]
    # Only the breaching bound; the within Brier bound is not listed.
    assert data["breaching_bounds"] == [
        {"bound": "maximum_expected_calibration_error", "measured": 0.12, "limit": 0.05}]
    assert data["summary"] == (
        "calibration threshold breach on svc: maximum_expected_calibration_error 0.12 > 0.05")
    # Existing fields are kept; SLO fields are not faked.
    assert data["threshold_key"] == a[0]["data"]["threshold_key"]
    assert (data["threshold_type"], data["severity"]) == ("calibration", "high")
    assert data["outcome"] == a[0]["data"]["outcome"]
    assert not {"slo_name", "current_value", "target"} & set(data)


def _spec_threshold(threshold_type):
    """The spec example's entry of this type, parsed as core serves it."""
    src = _SPEC_V2 / "examples" / "judgment-thresholds" / "all-five-types.yaml"
    (entry,) = [e for e in yaml.safe_load(src.read_text())["spec"]["judgment_threshold"]
                if e["type"] == threshold_type]
    return threshold_from_v2_entry(entry)


def test_a_lower_bound_breach_reads_as_at_least_the_limit():
    # A p50 beyond the histogram's top finite bucket (3600s) is AtLeast(3600):
    # a breach of the 1h bound decided from a lower bound (reason lower_bound).
    t = _spec_threshold("feedback_latency")
    outcome = evaluate_threshold(t, Samples(bounds={"maximum_p50": AtLeast(3600.0),
                                                    "maximum_p90": 1000.0}))
    assert outcome.bounds[0].reason == "lower_bound"
    rows, summary = breach_readout("svc", outcome)
    assert rows == [{"bound": "maximum_p50", "measured": 3600.0, "limit": "1h"}]
    assert summary == "feedback_latency threshold breach on svc: maximum_p50 3600s >= 1h (lower bound)"


def test_a_segments_breach_lists_each_breaching_segment():
    t = _spec_threshold("segments")   # enterprise, smb, consumer; bound 0.05
    outcome = evaluate_threshold(t, Samples(overall=0.1, segments={
        "enterprise": 0.3, "smb": 0.12, "consumer": 0.2}))
    rows, summary = breach_readout("svc", outcome)
    assert rows == [
        {"bound": "maximum_variance_from_overall", "segment": "enterprise", "measured": 0.2, "limit": 0.05},
        {"bound": "maximum_variance_from_overall", "segment": "consumer", "measured": 0.1, "limit": 0.05},
    ]
    assert summary == ("segments threshold breach on svc: "
                       "maximum_variance_from_overall (segment enterprise) 0.2 > 0.05")


def test_an_outcome_without_a_breaching_row_gets_the_bare_summary():
    outcome = evaluate_threshold(threshold_from_v2_entry(_CAL),
                                 Samples(bounds={"maximum_expected_calibration_error": 0.01}))
    assert breach_readout("svc", outcome) == ([], "calibration threshold breach on svc")


async def test_the_breaches_log_names_keys_and_services():
    m, prov, a, v = _module([0.2, 0.2], _manifests(("b", [_CAL]), ("a", [_CAL])))
    with structlog.testing.capture_logs() as logs:
        await _cycles(m, prov, 1)
    keys = sorted(x["data"]["threshold_key"] for x in _breaches(v))
    (event,) = [e for e in logs if e["event"] == "threshold_breaches"]
    assert (event["count"], sorted(event["keys"]), event["services"]) == (len(keys), keys, ["a", "b"])
    assert len(keys) == 2


# --- Failure containment and delivery (R5 edge-cases) ----------------------

_RR_SLO = {"name": "rr", "target": 98.5, "judgment_type": "reversal_rate", "window": "30d",
           "indicator_query": "q"}
_LAT = {"type": "feedback_latency", "bounds": {"maximum_p50": "1h"}}
_EMPTY = {"status": "success", "data": {"result": []}}


def _with_slo_breach(m, prov):
    """svc also declares an SLO that breaches (98.0 < 98.5)."""
    m.client.get_manifests = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=[
        {"name": "svc", "type": "ai-gate", "slos": [_RR_SLO], "judgment_thresholds": [_CAL]}]))
    prov.get_sli_value = AsyncMock(return_value=0.98)


def _slo_breaches(verdicts):
    return [x["data"].get("slo_name") for x in _breaches(verdicts) if "slo_name" in x["data"]]


async def test_failed_breach_verdict_refires_next_cycle():
    m, prov, a, v = _module([0.2, 0.2, 0.2], _manifests(("svc", [_CAL])))
    answers = [APIResult(ok=False, status_code=503), APIResult(ok=True, status_code=201, data={}),
               APIResult(ok=True, status_code=201, data={})]
    m.client.submit_verdict = AsyncMock(side_effect=lambda env: (v.append(env["data"]), answers.pop(0))[1])
    await _cycles(m, prov, 3)
    # cycle 1 rejected -> state unchanged; cycle 2 raises it again; cycle 3 nothing new.
    assert len(_breaches(v)) == 2


async def test_failed_assessment_still_raises_the_verdict():
    m, prov, a, v = _module([0.2], _manifests(("svc", [_CAL])))
    m.client.submit_assessment = AsyncMock(return_value=APIResult(ok=False, status_code=503))
    await _cycles(m, prov, 1)
    assert len(_breaches(v)) == 1


async def test_one_failing_entry_does_not_stop_the_others():
    from nthlayer_workers.measure.thresholds import phase

    real = phase.evaluate_threshold

    def flaky(t, samples):
        if t.type == "audit_backlog":
            raise RuntimeError("boom")
        return real(t, samples)

    backlog = {"type": "audit_backlog", "bounds": {"maximum_age": "24h"}}
    m, prov, a, v = _module([0.2], _manifests(("svc", [backlog, _CAL])))
    with patch("nthlayer_workers.measure.thresholds.phase.evaluate_threshold", side_effect=flaky):
        await _cycles(m, prov, 1)
    assert [x["data"]["outcome"]["type"] for x in a] == ["calibration"]
    assert len(_breaches(v)) == 1


async def test_a_raising_entry_is_contained_inside_the_phase():
    """The phase itself returns the good entry's breach and commits its state:
    the failure must not escape run_threshold_phase (which would cost this
    cycle's threshold breaches in governance), not merely spare a sibling."""
    from nthlayer_workers.measure.thresholds import phase

    real = phase.evaluate_threshold

    def flaky(t, samples):
        if t.type == "audit_backlog":
            raise RuntimeError("boom")
        return real(t, samples)

    backlog = {"type": "audit_backlog", "bounds": {"maximum_age": "24h"}}
    m, prov, a, v = _module([0.2], _manifests())
    state, problems = {}, set()
    with patch("nthlayer_workers.measure.thresholds.phase.evaluate_threshold", side_effect=flaky):
        breaches = await phase.run_threshold_phase(
            client=m.client, provider=prov, state=state, seen_problems=problems,
            manifests=[{"name": "svc", "judgment_thresholds": [backlog, _CAL]}],
        )
    good = threshold_key("svc", threshold_from_v2_entry(_CAL))
    assert [k for k, _svc, _id in breaches] == [good]
    assert set(state) == {good} and state[good]["last_evaluable"] == "breach"
    assert any(p.startswith("eval:") for p in problems)


async def test_threshold_phase_failure_does_not_stop_slo_phases():
    m, prov, a, v = _module([], _manifests(("svc", [_CAL])))
    _with_slo_breach(m, prov)
    with patch("nthlayer_workers.measure.worker.run_threshold_phase",
               AsyncMock(side_effect=RuntimeError("boom"))):
        await _cycles(m, prov, 1)
    assert _slo_breaches(v) == ["rr"]


async def test_a_raising_phase_waits_out_the_interval_before_retrying():
    """design §7 Cadence: the phase runs only when the interval has passed
    since the last run. A run that raised outright is still a run, so the
    next cycle inside the interval does not retry it (no log flood)."""
    m, prov, a, v = _module([], _manifests(("svc", [_CAL])), threshold_interval_seconds=900.0)
    phase = AsyncMock(side_effect=RuntimeError("boom"))
    with patch("nthlayer_workers.measure.worker.run_threshold_phase", phase), \
            patch("nthlayer_workers.measure.worker.PrometheusProvider", return_value=prov):
        m.clock.t = NOW
        await m.process_cycle()
        m.clock.t = NOW + 60
        await m.process_cycle()
        assert phase.await_count == 1
        m.clock.t = NOW + 900
        await m.process_cycle()
        assert phase.await_count == 2


@pytest.mark.parametrize("threshold_state", [
    None,
    "garbage",
    {"svc:calibration:abc": "not-a-dict"},
    {"svc:calibration:abc": {"last_evaluable": 7, "armed": "yes", "within_since": "x", "service": "svc",
                             "last_signature": 3, "last_emitted_at": True}},
    {"svc:calibration:abc": {"last_evaluable": "breach", "service": ["svc"]}},  # unhashable service
    {"svc:calibration:abc": {"last_evaluable": "breach"}},                      # no service
])
async def test_corrupt_restored_state_does_not_stop_the_cycle(threshold_state):
    m, prov, a, v = _module([0.2], _manifests(("svc", [_CAL])))
    _with_slo_breach(m, prov)
    await m.restore_state({"slo_status": {}, "threshold_state": threshold_state})
    await _cycles(m, prov, 1)
    assert _slo_breaches(v) == ["rr"]
    assert any(x["data"].get("threshold_type") == "calibration" for x in _breaches(v))
    assert all(isinstance(st, dict) and isinstance(st.get("service"), str)
               for st in m._threshold_state.values())
    assert "svc:calibration:abc" not in m._threshold_state


async def test_non_conforming_state_entry_is_dropped_by_the_phase():
    # Injected after restore (restore_state already filters it): the phase
    # itself must not raise on it, and must drop it.
    m, prov, a, v = _module([0.2], _manifests(("svc", [_CAL])))
    m._threshold_state = {"bad": {"service": ["svc"]}}
    await _cycles(m, prov, 1)
    assert "bad" not in m._threshold_state
    assert len(_breaches(v)) == 1


@pytest.mark.parametrize("bad", ["not-a-dict", None, {"name": ["svc"]}, {"name": ""}, {"type": "ai-gate"}])
async def test_malformed_manifest_is_skipped(bad):
    # Called directly: MeasureModule's existing SLO phase reads the same list
    # and is outside this bead; the threshold phase must not raise on it.
    from nthlayer_workers.measure.thresholds.phase import run_threshold_phase

    m, prov, a, v = _module([0.2], _manifests())
    problems: set[str] = set()
    breaches = await run_threshold_phase(
        client=m.client, provider=prov, state={}, seen_problems=problems,
        manifests=[bad, {"name": "svc", "type": "ai-gate", "slos": [], "judgment_thresholds": [_CAL]}],
    )
    assert [x["data"]["outcome"]["type"] for x in a] == ["calibration"]
    assert len(breaches) == 1
    assert any(p.startswith("manifest:0:") for p in problems)


@pytest.mark.parametrize("items", [5, True, "x", {"a": 1}])
async def test_non_list_thresholds_are_skipped_and_keep_state(items):
    from nthlayer_workers.measure.thresholds.phase import run_threshold_phase

    m, prov, a, v = _module([0.2], _manifests())
    state = {"bad:calibration:abc": {"last_evaluable": "breach", "service": "bad"}}
    breaches = await run_threshold_phase(
        client=m.client, provider=prov, state=state, seen_problems=set(),
        manifests=[{"name": "bad", "judgment_thresholds": items},
                   {"name": "svc", "judgment_thresholds": [_CAL]}],
    )
    assert len(breaches) == 1
    assert "bad:calibration:abc" in state  # not retired on a malformed response


def _recording_provider(answer):
    prov = AsyncMock()
    prov.seen = []

    async def query(q, time=None):
        prov.seen.append(q)
        return answer(q)

    prov.query = AsyncMock(side_effect=query)
    prov.aclose = AsyncMock()
    return prov


@pytest.mark.parametrize("env,expected", [(None, 1), ("on", 1), ("off", 0), ("FALSE", 0), ("0", 0)])
async def test_retention_is_queried_once_per_cycle_unless_switched_off(monkeypatch, env, expected):
    if env is None:
        monkeypatch.delenv("MEASURE_RETENTION_CHECK", raising=False)
    else:
        monkeypatch.setenv("MEASURE_RETENTION_CHECK", env)
    m, _, a, v = _module([], _manifests(("a", [_LAT]), ("b", [_LAT])))  # reads the env
    prov = _recording_provider(lambda q: _EMPTY)
    await _cycles(m, prov, 1)
    assert sum("prometheus_tsdb_lowest_timestamp_seconds" in q for q in prov.seen) == expected
    assert len(a) == 2


async def test_entries_are_evaluated_concurrently_up_to_eval_concurrency():
    active = peak = 0

    async def slow(q, time=None):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return _vector(0.01)

    m, _, a, v = _module([], _manifests(*[(f"s{i}", [_CAL]) for i in range(6)]), eval_concurrency=2)
    prov = AsyncMock()
    prov.query = AsyncMock(side_effect=slow)
    prov.aclose = AsyncMock()
    await _cycles(m, prov, 1)
    assert len(a) == 6 and peak == 2


# --- Cadence, on-change emission, re-arm duration (Checkpoint A decisions) --

def _evals(assessments):
    return [x for x in assessments if x["kind"] == "judgment_threshold_evaluation"]


async def test_unchanged_outcome_is_reemitted_only_on_the_heartbeat():
    m, prov, a, v = _module([0.01] * 4, _manifests(("svc", [_CAL])), threshold_heartbeat_seconds=3600.0)
    for t in (0, 900, 3600, 4500):
        m.clock.t = NOW + t
        await _cycles(m, prov, 1)
    # first sight (t=0) and the heartbeat (t=3600); 900 and 4500 are unchanged.
    assert len(_evals(a)) == 2
    assert m._threshold_state[threshold_key("svc", threshold_from_v2_entry(_CAL))]["last_emitted_at"] == NOW + 3600


async def test_a_changed_outcome_is_emitted_at_once():
    m, prov, a, v = _module([0.01, 0.2, 0.2], _manifests(("svc", [_CAL])))
    await _cycles(m, prov, 3)
    assert [x["data"]["outcome"]["status"] for x in _evals(a)] == ["within", "breach"]


async def test_measured_value_changes_alone_do_not_emit():
    m, prov, a, v = _module([0.01, 0.02, 0.03], _manifests(("svc", [_CAL])))
    await _cycles(m, prov, 3)
    assert len(_evals(a)) == 1



_SEGMENTS = {"type": "segments", "metric": "escalation", "segment_dimension": "region",
             "segment_values": ["eu", "us"], "bounds": {"maximum_variance_from_overall": 0.05}}


@pytest.mark.parametrize("a_segments,b_segments", [
    # Which segment is evaluable differs; type, combined status and the bound row do not.
    ({"eu": 0.11, "us": Missing("no_data", "x")}, {"eu": Missing("no_data", "x"), "us": 0.11}),
    # Only one segment's reason differs (the bound row takes the first segment's).
    ({"eu": Missing("no_data", "x"), "us": Missing("no_data", "x")},
     {"eu": Missing("no_data", "x"), "us": Missing("no_data_source", "x")}),
])
def test_signature_includes_per_segment_status_and_reason(a_segments, b_segments):
    """design §7 item 5: the signature carries per-segment (segment, status,
    reason), so a change confined to the segment rows is re-emitted."""
    t = threshold_from_v2_entry(_SEGMENTS)
    a = evaluate_threshold(t, Samples(overall=0.1, segments=a_segments))
    b = evaluate_threshold(t, Samples(overall=0.1, segments=b_segments))
    assert (a.type, a.status, a.bounds[0].status, a.bounds[0].reason) == (
        b.type, b.status, b.bounds[0].status, b.bounds[0].reason)       # only the segments differ
    assert a.segments != b.segments
    assert outcome_signature(a) != outcome_signature(b)

async def test_a_firing_breach_always_emits_its_parent_assessment():
    """Even with the same signature as the last emission and inside the
    heartbeat: the verdict's parent_ids must name an assessment from this run."""
    m, prov, a, v = _module([0.2, 0.2], _manifests(("svc", [_CAL])))
    await _cycles(m, prov, 1)
    key = threshold_key("svc", threshold_from_v2_entry(_CAL))
    m._threshold_state[key].update(last_evaluable="within", armed=True)  # re-armed since
    await _cycles(m, prov, 1)
    evals, breaches = _evals(a), _breaches(v)
    assert len(evals) == 2 and len(breaches) == 2
    assert breaches[1]["parent_ids"] == [evals[1]["id"]]


async def test_the_phase_runs_on_its_own_interval():
    m, prov, a, v = _module([0.01, 0.01], _manifests(("svc", [_CAL])), threshold_interval_seconds=900.0)
    await _cycles(m, prov, 1)        # first cycle after start: runs
    m.clock.t = NOW + 300
    await _cycles(m, prov, 1)        # inside the interval: does not run
    assert prov.query.await_count == 1
    m.clock.t = NOW + 900
    await _cycles(m, prov, 1)        # 900s after the last run: runs
    assert prov.query.await_count == 2


async def test_the_interval_survives_a_restart():
    m, prov, a, v = _module([0.01], _manifests(("svc", [_CAL])), threshold_interval_seconds=900.0)
    await _cycles(m, prov, 1)
    saved = await m.get_state()
    m2, prov2, *_ = _module([0.01], _manifests(("svc", [_CAL])), threshold_interval_seconds=900.0)
    await m2.restore_state(saved)
    m2.clock.t = NOW + 300
    await _cycles(m2, prov2, 1)
    assert prov2.query.await_count == 0


@pytest.mark.parametrize("last_run", [None, "x", True, float("nan")])
async def test_a_corrupt_last_run_runs_the_phase(last_run):
    m, prov, a, v = _module([0.01], _manifests(("svc", [_CAL])), threshold_interval_seconds=900.0)
    await m.restore_state({"slo_status": {}, "threshold_last_run": last_run})
    await _cycles(m, prov, 1)
    assert prov.query.await_count == 1


@pytest.mark.parametrize("statuses,expected_breaches", [
    # One evaluation per 100s interval; re-arm needs 3 x 100s within.
    ([0.2, 0.01, 0.01, 0.01, 0.2], 1),          # within 100..300: 200s, not re-armed
    ([0.2, 0.01, 0.01, 0.01, 0.01, 0.2], 2),    # within 100..400: 300s, re-armed
])
async def test_rearm_is_three_threshold_intervals(statuses, expected_breaches):
    m, prov, a, v = _module(statuses, _manifests(("svc", [_CAL])), threshold_interval_seconds=100.0)
    await _cycles(m, prov, len(statuses))  # _cycles moves the clock 100s per cycle
    assert len(_breaches(v)) == expected_breaches


async def test_rearm_needs_three_within_evaluations_even_when_far_apart():
    """A 1s interval makes the 3s duration pass at once when evaluations are
    10s apart; the within count still holds the key disarmed."""
    m, prov, a, v = _module([0.2, 0.01, 0.01, 0.2], _manifests(("svc", [_CAL])),
                            threshold_interval_seconds=1.0)
    for t in (0, 10, 20, 30):
        m.clock.t = NOW + t
        await _cycles(m, prov, 1)
    assert len(_breaches(v)) == 1


async def test_a_backward_clock_still_runs_emits_and_rearms():
    m, prov, a, v = _module([0.2, 0.01, 0.01, 0.01, 0.01, 0.01, 0.2], _manifests(("svc", [_CAL])),
                            threshold_interval_seconds=60.0)
    times = [10_000, 10_060, 100, 160, 220, 280, 340]  # the clock goes back ~10000s
    for t in times:
        m.clock.t = NOW + t
        await _cycles(m, prov, 1)
    # (c) every cycle was a threshold run, the backward jump included (now < last run).
    assert prov.query.await_count == len(times)
    # (b) the jump re-emitted the unchanged within outcome (its last emission is in the future).
    assert [x["data"]["outcome"]["status"] for x in _evals(a)] == ["breach", "within", "within", "breach"]
    # (a) the within run restarted at 100: by 280 it is 180s = 3 x 60s old with 4 evaluations,
    # so the key re-armed and the breach at 340 fires.
    assert len(_breaches(v)) == 2


async def test_tightening_a_bound_emits_without_a_status_change():
    tighter = dict(_CAL, bounds={"maximum_expected_calibration_error": 0.03})
    m, prov, a, v = _module([0.01, 0.01], [_manifests(("svc", [_CAL])), _manifests(("svc", [tighter]))])
    await _cycles(m, prov, 2)
    assert [(x["data"]["outcome"]["status"], x["data"]["outcome"]["bounds"][0]["limit"]) for x in _evals(a)] == [
        ("within", 0.05), ("within", 0.03)]


async def test_a_removed_entry_retires_on_a_non_threshold_cycle():
    m, prov, a, v = _module([0.2], [_manifests(("svc", [_CAL])), _manifests(("svc", []))],
                            threshold_interval_seconds=900.0)
    await _cycles(m, prov, 1)
    m.clock.t = NOW + 60             # inside the interval: no threshold run...
    await _cycles(m, prov, 1)
    assert prov.query.await_count == 1
    assert m._threshold_state == {}  # ...but the removed entry retired


_SEG = {"type": "segments", "metric": "escalation", "segment_dimension": "region",
        "segment_values": ["eu", "us"], "bounds": {"maximum_variance_from_overall": 0.05}}


async def test_segment_value_order_does_not_make_a_distinct_entry():
    m, prov, a, v = _module([], _manifests(("svc", [_SEG, dict(_SEG, segment_values=["us", "eu"])])))
    await _cycles(m, prov, 1)
    assert len(_evals(a)) == 1
    assert set(m._threshold_state) == {threshold_key("svc", threshold_from_v2_entry(_SEG))}


@pytest.mark.parametrize("name,default", [("MEASURE_THRESHOLD_INTERVAL", 900.0),
                                          ("MEASURE_THRESHOLD_HEARTBEAT", 3600.0)])
@pytest.mark.parametrize("raw,expected", [
    (None, None), ("60", 60.0), ("604800", 604800.0),                    # None: the default
    ("abc", None), ("0", None), ("-5", None), ("nan", None), ("inf", None),
    ("1e308", None), ("604801", None),
])
def test_threshold_timing_env(monkeypatch, name, default, raw, expected):
    if raw is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, raw)
    m = MeasureModule(client=AsyncMock(), prometheus_url="http://p")
    field = {"MEASURE_THRESHOLD_INTERVAL": m.threshold_interval_seconds,
             "MEASURE_THRESHOLD_HEARTBEAT": m.threshold_heartbeat_seconds}[name]
    assert field == (default if expected is None else expected)


# --- Review fixes: unparseable entries keep state; failed emission retries --

# Fails threshold_from_v2_entry: a bound value must be a number.
_CAL_BROKEN = dict(_CAL, bounds={"maximum_expected_calibration_error": "bad"})


async def test_an_entry_that_stops_parsing_keeps_its_open_breach():
    m, prov, a, v = _module([0.2, 0.2], [
        _manifests(("svc", [_CAL])),
        _manifests(("svc", [_CAL_BROKEN])),   # same entry, now unparseable
        _manifests(("svc", [_CAL])),          # parses again
    ])
    key = threshold_key("svc", threshold_from_v2_entry(_CAL))
    await _cycles(m, prov, 1)
    after_breach = dict(m._threshold_state[key])
    assert after_breach["last_evaluable"] == "breach" and after_breach["armed"] is False
    await _cycles(m, prov, 1)
    assert m._threshold_state[key] == after_breach   # not retired, untouched
    await _cycles(m, prov, 1)
    assert len(_breaches(v)) == 1                    # no duplicate on re-parse


def test_retire_thresholds_keeps_state_of_a_service_with_an_unparseable_entry():
    from nthlayer_workers.measure.thresholds.phase import retire_thresholds

    key = threshold_key("svc", threshold_from_v2_entry(_CAL))
    entry = {"last_evaluable": "breach", "armed": False, "within_since": None,
             "within_count": 0, "service": "svc"}
    state = {key: dict(entry)}
    retire_thresholds(manifests=[{"name": "svc", "judgment_thresholds": [_CAL_BROKEN]}],
                      state=state, seen_problems=set())
    assert state == {key: entry}
    # Control: a clean manifest without the entry does retire it.
    retire_thresholds(manifests=[{"name": "svc", "judgment_thresholds": []}],
                      state=state, seen_problems=set())
    assert state == {}


async def test_a_rejected_assessment_is_not_recorded_as_emitted():
    m, prov, a, v = _module([0.01, 0.01], _manifests(("svc", [_CAL])),
                            threshold_heartbeat_seconds=3600.0)
    answers = [APIResult(ok=False, status_code=503), APIResult(ok=True, status_code=201, data={})]
    m.client.submit_assessment = AsyncMock(side_effect=lambda env: answers.pop(0))
    await _cycles(m, prov, 2)  # within both times, same signature, inside the heartbeat
    assert m.client.submit_assessment.await_count == 2
    assert v == []
