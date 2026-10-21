"""MEASURE_THRESHOLD_AUTONOMY: threshold breaches feed the autonomy ratchet (opensrm-g32d)."""

from unittest.mock import AsyncMock, patch

import pytest
from nthlayer_common.api_client import APIResult

from nthlayer_workers.measure.worker import MeasureModule

_CAL = {"type": "calibration", "measurement": {"bins": 10, "window": "30d"},
        "bounds": {"maximum_expected_calibration_error": 0.05}}


def _setup(monkeypatch, env):
    if env is None:
        monkeypatch.delenv("MEASURE_THRESHOLD_AUTONOMY", raising=False)
    else:
        monkeypatch.setenv("MEASURE_THRESHOLD_AUTONOMY", env)
    client = AsyncMock()
    client.get_manifests = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=[
        {"name": "svc", "type": "ai-gate", "slos": [], "judgment_thresholds": [_CAL]}]))
    client.submit_assessment = AsyncMock(return_value=APIResult(ok=True, status_code=201, data={}))
    verdicts = []
    client.submit_verdict = AsyncMock(side_effect=lambda v: (verdicts.append(v["data"]),
                                      APIResult(ok=True, status_code=201, data={}))[1])
    prov = AsyncMock()
    prov.query = AsyncMock(return_value={"data": {"result": [{"value": [0, "0.2"]}]}})
    prov.aclose = AsyncMock()
    clock = _Clock()
    return MeasureModule(client=client, prometheus_url="http://p", clock=clock), prov, verdicts


class _Clock:
    """MeasureModule.clock: the threshold phase reruns only after
    threshold_interval_seconds (default 900), so tests move it explicitly."""

    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


async def _cycle(m, prov):
    with patch("nthlayer_workers.measure.worker.PrometheusProvider", return_value=prov):
        await m.process_cycle()


@pytest.mark.parametrize("env", [None, "", "0", "false", "off"])
async def test_off_by_default_no_ratchet(monkeypatch, env):
    m, prov, verdicts = _setup(monkeypatch, env)
    await _cycle(m, prov)
    assert [v for v in verdicts if v["type"] == "autonomy_change"] == []
    assert m._autonomy_levels.get("svc", "fully_autonomous") == "fully_autonomous"


async def test_off_leaves_governance_state_exactly_as_before(monkeypatch):
    """With the toggle off, a real threshold breach (its quality_breach is
    sent) changes no governance state across threshold and non-threshold
    cycles: behaviour is exactly that before the toggle existed."""
    m, prov, verdicts = _setup(monkeypatch, None)
    assert m.threshold_autonomy is False
    await _cycle(m, prov)
    m.clock.t += 60                           # a non-threshold cycle
    await _cycle(m, prov)
    m.clock.t += 900                          # another threshold run, breach still open
    await _cycle(m, prov)
    # Non-vacuous: the threshold did breach, exactly once.
    assert [v["type"] for v in verdicts] == ["quality_breach"]
    assert m._threshold_state and all(
        st["last_evaluable"] == "breach" for st in m._threshold_state.values())
    state = await m.get_state()
    assert state["slo_status"] == {}
    assert state["breach_decisions"] == {}
    assert state["breach_severities"] == {}
    assert state["autonomy_levels"] == {}
    assert state["threshold_governed"] == []


@pytest.mark.parametrize("env", ["1", "true", "YES", "on"])
async def test_on_feeds_the_ratchet_as_a_high_breach(monkeypatch, env):
    m, prov, verdicts = _setup(monkeypatch, env)
    await _cycle(m, prov)
    changes = [v for v in verdicts if v["type"] == "autonomy_change"]
    assert len(changes) == 1
    assert m._autonomy_levels["svc"] == "limited_autonomous"  # high single breach: 2 steps


async def test_enabling_later_does_not_ratchet_an_open_breach(monkeypatch):
    m, prov, verdicts = _setup(monkeypatch, None)
    await _cycle(m, prov)
    m.threshold_autonomy = True
    m.clock.t += 900                          # a threshold run: the breach is still open
    await _cycle(m, prov)
    assert [v for v in verdicts if v["type"] == "autonomy_change"] == []


async def test_open_threshold_breach_counts_in_later_governance(monkeypatch):
    """An open threshold breach plus a later SLO breach is a multiple-breach
    case (advisor floor), as two SLO breaches would be."""
    m, prov, verdicts = _setup(monkeypatch, "1")
    await _cycle(m, prov)                     # threshold breach decided (open)
    # Reset the level so the outcome separates the two behaviours:
    # with re-injection 2 breaches -> advisor floor; without, 1 low breach -> autonomous.
    m._autonomy_levels["svc"] = "fully_autonomous"
    queries_before = prov.query.await_count
    m.clock.t += 60                           # inside the interval: a non-threshold cycle
    m.client.get_manifests = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=[
        {"name": "svc", "type": "ai-gate", "judgment_thresholds": [_CAL], "slos": [
            {"name": "rr", "target": 98.5, "judgment_type": "reversal_rate", "window": "30d",
             "indicator_query": "q"}]}]))
    # A mild SLO breach (98.0 < 98.5 target; 33% budget consumption = low), so
    # the advisor floor below comes from the breach COUNT, not from severity.
    prov.get_sli_value = AsyncMock(return_value=0.98)
    await _cycle(m, prov)
    assert prov.query.await_count == queries_before  # the threshold phase did not run...
    assert m._autonomy_levels["svc"] == "advisor"    # ...yet its open breach counted
    last_change = [v for v in verdicts if v["type"] == "autonomy_change"][-1]
    assert last_change["data"]["breaching_slo_count"] == 2


async def test_open_slo_breach_keeps_its_governance_memory(monkeypatch):
    """Threshold cleanup must touch only threshold keys: an open SLO breach's
    decision survives a cycle with the toggle on."""
    m, prov, verdicts = _setup(monkeypatch, "1")
    m.client.get_manifests = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=[
        {"name": "svc", "type": "ai-gate", "judgment_thresholds": [], "slos": [
            {"name": "rr", "target": 98.5, "judgment_type": "reversal_rate", "window": "30d",
             "indicator_query": "q"}]}]))
    prov.get_sli_value = AsyncMock(return_value=0.98)
    await _cycle(m, prov)
    await _cycle(m, prov)
    assert m._breach_decisions.get("svc:rr", {}).get("decided")


async def test_retired_threshold_leaves_no_governance_memory(monkeypatch):
    m, prov, verdicts = _setup(monkeypatch, "1")
    await _cycle(m, prov)
    m.client.get_manifests = AsyncMock(return_value=APIResult(ok=True, status_code=200, data=[
        {"name": "svc", "type": "ai-gate", "slos": [], "judgment_thresholds": []}]))
    # No clock move: a non-threshold cycle retires the removed entry too
    # (retire_thresholds runs on every cycle the phase does not).
    await _cycle(m, prov)
    assert not any(":calibration:" in k for k in m._breach_decisions)
    assert not any(":calibration:" in k for k in m._breach_severities)


@pytest.mark.parametrize("raw,expected", [
    (None, set()), ("svc:calibration:abc", set()), ({"k": 1}, set()),
    (["svc:calibration:abc", 7], {"svc:calibration:abc"}),
])
async def test_corrupt_threshold_governed_is_coerced(monkeypatch, raw, expected):
    m, prov, verdicts = _setup(monkeypatch, "1")
    await m.restore_state({"slo_status": {}, "threshold_governed": raw})
    assert m._threshold_governed == expected
    await _cycle(m, prov)  # and the cycle still runs


async def test_threshold_keys_never_enter_slo_status(monkeypatch):
    """_slo_status stays SLO-only: a threshold key carried forward there would
    outlive its retirement and count as a breach forever."""
    m, prov, verdicts = _setup(monkeypatch, "1")
    await _cycle(m, prov)                     # new threshold breach
    m.clock.t += 60
    await _cycle(m, prov)                     # open breach re-injected
    assert m._breach_decisions and m._threshold_governed  # non-vacuous: it was governed
    assert not any(":calibration:" in k for k in m._slo_status)
    assert not any(":calibration:" in k for k in (await m.get_state())["slo_status"])


async def test_recovered_threshold_leaves_governance_but_keeps_the_ratchet(monkeypatch):
    m, prov, verdicts = _setup(monkeypatch, "1")
    await _cycle(m, prov)
    assert m._threshold_governed
    prov.query = AsyncMock(return_value={"data": {"result": [{"value": [0, "0.01"]}]}})
    m.clock.t += 900                          # a threshold run: now within bounds
    await _cycle(m, prov)
    assert all(st["last_evaluable"] == "within" for st in m._threshold_state.values())
    assert m._threshold_governed == set()
    assert not any(":calibration:" in k for k in m._breach_decisions)
    assert not any(":calibration:" in k for k in m._breach_severities)
    assert m._autonomy_levels["svc"] == "limited_autonomous"  # one-way ratchet
