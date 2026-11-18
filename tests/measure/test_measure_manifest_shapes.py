"""MeasureModule against /manifests responses that are malformed or empty.

Two things (opensrm-jarh):

1. One entry of the wrong shape costs only itself. The SLO phase used to
   index every entry as a dict, so a single bad one raised out of
   process_cycle and nothing ran for any service.

2. An empty successful response changes nothing. Threshold state and
   governance of a service are kept while it is absent from a response
   (tests/measure/thresholds/test_phase.py,
   test_absent_service_keeps_state_and_removed_entry_retires), and an empty
   list is every service absent.

PROVENANCE OF THE SHAPES. The well-formed manifest is what nthlayer-core
serves: ``catalogue.to_dict_list()`` is a list of ``manifest_to_dict``
dicts, each with a string ``name`` and a list ``slos``. The malformed
entries are NOT shapes core produces; no core version serves them. They
stand for a response that is not what core sends (a proxy's error body, a
different server behind the URL), which is what the guard is for.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import structlog
from nthlayer_common.api_client import APIResult

from nthlayer_workers.measure.worker import MeasureModule, _judgment_slos

_JUDGMENT_SLO = {
    "name": "reversal_rate",
    "target": 98.5,
    "slo_type": "availability",
    "window": "2m",
    "judgment_type": "reversal_rate",
    "indicator_query": "1 - (sum(rate(nthlayer_reversals_total[2m]))"
                       " / clamp_min(sum(rate(nthlayer_decisions_total[2m])), 1))",
}
# A judgment threshold as core 3.0+ serves it under judgment_thresholds.
_CALIBRATION = {
    "type": "calibration",
    "measurement": {"bins": 10, "window": "30d"},
    "bounds": {"maximum_expected_calibration_error": 0.05},
}


def _manifest(name: str = "fraud-detect", **over) -> dict:
    return {
        "name": name, "tier": "critical", "type": "ai-gate",
        "slos": [dict(_JUDGMENT_SLO)], "judgment_thresholds": [dict(_CALIBRATION)],
        "dependencies": [], "contracts": [],
        **over,
    }


# Each malformed entry with the path the skip is logged under, when it stands
# at index 1 of the response (index 0 is the well-formed manifest).
MALFORMED = [
    pytest.param("not-a-manifest", "/manifests[1]", id="string-entry"),
    pytest.param(None, "/manifests[1]", id="null-entry"),
    pytest.param(["fraud-detect"], "/manifests[1]", id="list-entry"),
    pytest.param({"tier": "critical", "slos": []}, "/manifests[1]", id="no-name"),
    pytest.param({"name": "", "slos": []}, "/manifests[1]", id="empty-name"),
    pytest.param({"name": 7, "slos": []}, "/manifests[1]", id="non-string-name"),
    pytest.param({"name": "other", "slos": {"reversal_rate": {}}}, "/manifests[1].slos",
                 id="slos-is-a-mapping"),
    pytest.param({"name": "other", "slos": "none"}, "/manifests[1].slos", id="slos-is-a-string"),
    pytest.param({"name": "other", "slos": ["reversal_rate"]}, "/manifests[1].slos[0]",
                 id="slo-is-a-string"),
    pytest.param({"name": "other", "slos": [None]}, "/manifests[1].slos[0]", id="slo-is-null"),
]


class _Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def _vector(v: float) -> dict:
    return {"status": "success", "data": {"result": [{"metric": {}, "value": [0, str(v)]}]}}


def _module(responses: list, *, ece: float = 0.01, **module_kw):
    """A MeasureModule whose client serves *responses* in turn, with what it
    submitted, and the Prometheus stand-in: the judgment SLI is healthy
    (0.99) and every threshold query returns *ece*."""
    client = AsyncMock()
    client.get_manifests = AsyncMock(
        side_effect=[APIResult(ok=True, status_code=200, data=r) for r in responses]
    )
    assessments: list[dict] = []
    verdicts: list[dict] = []
    client.submit_assessment = AsyncMock(side_effect=lambda a: (
        assessments.append(a["data"]), APIResult(ok=True, status_code=201, data={}))[1])
    client.submit_verdict = AsyncMock(side_effect=lambda v: (
        verdicts.append(v["data"]), APIResult(ok=True, status_code=201, data={}))[1])
    prov = AsyncMock()
    prov.get_sli_value = AsyncMock(return_value=0.99)
    prov.query = AsyncMock(return_value=_vector(ece))
    prov.aclose = AsyncMock()
    module_kw.setdefault("threshold_interval_seconds", 1.0)
    module_kw.setdefault("clock", _Clock())
    module = MeasureModule(client=client, prometheus_url="http://prom:9090", **module_kw)
    return module, prov, assessments, verdicts


async def _cycle(module, prov):
    """One process_cycle, then the clock moves on one threshold interval.
    Returns the patched PrometheusProvider class, to see whether the cycle
    got as far as building a provider."""
    with patch("nthlayer_workers.measure.worker.PrometheusProvider", return_value=prov) as cls:
        await module.process_cycle()
    module.clock.t += module.threshold_interval_seconds
    return cls


# --- 1. one malformed entry costs only itself -------------------------------


@pytest.mark.parametrize(("bad", "where"), MALFORMED)
async def test_a_malformed_entry_does_not_stop_the_cycle(bad, where):
    """The well-formed service beside it is still evaluated by the SLO phase
    AND reached by the threshold phase: before the fix the cycle raised in
    the SLO phase, so neither happened."""
    module, prov, assessments, _ = _module([[_manifest(), bad]])

    with structlog.testing.capture_logs() as logs:
        await _cycle(module, prov)

    assert [(a["kind"], a["service"]) for a in assessments
            if a["kind"] == "judgment_slo_evaluation"] == [
        ("judgment_slo_evaluation", "fraud-detect")
    ]
    assert [a["service"] for a in assessments
            if a["kind"] == "judgment_threshold_evaluation"] == ["fraud-detect"]
    assert module._slo_status == {"fraud-detect:reversal_rate": "healthy"}
    skipped = [e for e in logs if e["event"] == "measure_manifest_skipped"]
    assert [(e["log_level"], e["where"]) for e in skipped] == [("warning", where)]


@pytest.mark.parametrize(("bad", "where"), MALFORMED)
async def test_a_malformed_entry_is_logged_once_not_every_cycle(bad, where):
    module, prov, _, _ = _module([[_manifest(), bad]] * 3)

    with structlog.testing.capture_logs() as logs:
        for _ in range(3):
            await _cycle(module, prov)

    assert [e["where"] for e in logs if e["event"] == "measure_manifest_skipped"] == [where]


async def test_each_distinct_problem_is_logged_not_just_the_first():
    """Once per DISTINCT problem. Two different malformed entries in one
    response are two problems; remembering only that something was skipped
    would log the first and hide every later one for the life of the
    process (R5 provenance, opensrm-jarh)."""
    response = [_manifest(), "not-a-manifest", {"name": "other", "slos": "none"},
                {"name": "third", "slos": [None]}]
    module, prov, _, _ = _module([response, response])

    with structlog.testing.capture_logs() as logs:
        await _cycle(module, prov)
        await _cycle(module, prov)

    assert [e["where"] for e in logs if e["event"] == "measure_manifest_skipped"] == [
        "/manifests[1]", "/manifests[2].slos", "/manifests[3].slos[0]",
    ]


@pytest.mark.parametrize(
    "manifest",
    [
        pytest.param(_manifest(slos=None), id="slos-null"),
        pytest.param({k: v for k, v in _manifest().items() if k != "slos"}, id="slos-absent"),
        pytest.param(_manifest(slos=[]), id="slos-empty"),
    ],
)
async def test_a_manifest_with_no_slos_is_not_a_problem(manifest):
    """No SLOs to evaluate is ordinary, and is not logged as a skip."""
    module, prov, assessments, _ = _module([[manifest]])

    with structlog.testing.capture_logs() as logs:
        await _cycle(module, prov)

    assert [a for a in assessments if a["kind"] == "judgment_slo_evaluation"] == []
    assert [e for e in logs if e["event"] == "measure_manifest_skipped"] == []


def test_judgment_slos_returns_only_judgment_slos_of_well_formed_entries():
    classical = {"name": "availability", "target": 99.9, "slo_type": "availability"}
    manifests = [
        _manifest(slos=[dict(_JUDGMENT_SLO), classical]),
        "not-a-manifest",
        _manifest("second", slos=[dict(_JUDGMENT_SLO, name="hcf", judgment_type="high_confidence_failure")]),
    ]

    found = _judgment_slos(manifests, set())

    assert [(service, slo["name"]) for service, slo in found] == [
        ("fraud-detect", "reversal_rate"), ("second", "hcf"),
    ]


@pytest.mark.parametrize(("bad", "where"), MALFORMED)
def test_find_slo_passes_over_a_malformed_entry(bad, where):
    """_find_slo runs in Phase 2, on a new breach. It reads the same response,
    so it must survive the same entries, whichever side of the match they
    are on."""
    for manifests in ([bad, _manifest()], [_manifest(), bad]):
        assert MeasureModule._find_slo(manifests, "fraud-detect", "reversal_rate") == _JUDGMENT_SLO
        assert MeasureModule._find_slo(manifests, "other", "reversal_rate") is None


@pytest.mark.parametrize("slos", [None, 7, 0.5, True], ids=["null", "int", "float", "bool"])
def test_find_slo_passes_over_a_slos_that_cannot_be_iterated(slos):
    """The entries above leave a `slos` that is a string or a mapping, which
    iterate without harm. One that is null or a number does not iterate at
    all, and it is found under the very service being looked up: an earlier
    entry for the same name (R5 provenance, opensrm-jarh)."""
    manifests = [{"name": "fraud-detect", "slos": slos}, _manifest()]

    assert MeasureModule._find_slo(manifests, "fraud-detect", "reversal_rate") == _JUDGMENT_SLO
    assert MeasureModule._find_slo(manifests[:1], "fraud-detect", "reversal_rate") is None


# --- a body that is not a list at all ---------------------------------------


@pytest.mark.parametrize(
    ("body", "body_type"),
    [
        pytest.param({"error": "bad_gateway", "detail": {"upstream": "core"}}, "dict", id="error-object"),
        pytest.param("<html><body>502 Bad Gateway</body></html>", "str", id="text"),
        pytest.param(1, "int", id="number"),
        pytest.param(True, "bool", id="true"),
        pytest.param(0.5, "float", id="float"),
    ],
)
async def test_a_body_that_is_not_a_list_ends_the_cycle_and_changes_nothing(body, body_type):
    """Whole-response shapes, as the entries above are per-entry ones: what a
    proxy or a wrong server answers with 200. None is a body core sends.

    Each is treated as a failed fetch. Walked as a list of manifests, an
    error object's keys or a page's characters were logged as skipped
    entries and a due threshold run was spent on them; a number raised out
    of the cycle (R5 edge cases, opensrm-jarh).
    """
    module, prov, assessments, verdicts = _module([[_manifest()], body, body])
    await _cycle(module, prov)
    before = await module.get_state()
    submitted_before = (list(assessments), list(verdicts))

    with structlog.testing.capture_logs() as logs:
        first = await _cycle(module, prov)
        second = await _cycle(module, prov)

    assert await module.get_state() == before
    assert (assessments, verdicts) == submitted_before
    # Ended before a provider was built, so before the threshold phase could
    # take its turn: threshold_last_run is part of the state compared above.
    assert (first.call_count, second.call_count) == (0, 0)
    # Said once for the two cycles, and not as a list of skipped entries.
    assert [(e["event"], e["log_level"], e["type"]) for e in logs
            if e["event"].startswith("measure_manifest")] == [
        ("measure_manifests_not_a_list", "warning", body_type)
    ]


# --- 2. an empty successful response changes nothing ------------------------


async def test_an_empty_response_leaves_threshold_state_and_governance_unchanged():
    """A service with an open threshold breach that governance has acted on.
    /manifests then answers 200 with an empty list (a core that has just
    restarted with nothing loaded yet, say), and then serves the service
    again.

    Nothing may move on the empty cycle: not the threshold state, not the
    governance entries, not the autonomy level, and not the threshold
    cadence. And when the service returns, the breach must not fire a second
    time: the autonomy ratchet is one-way, so a second firing would be a
    second reduction for one breach.
    """
    serving = [_manifest()]
    module, prov, _, verdicts = _module(
        [serving, [], serving], ece=0.2, threshold_autonomy=True,
    )

    await _cycle(module, prov)
    before = await module.get_state()
    # The premise, or the rest proves nothing: an open breach, governed.
    assert before["threshold_governed"] != []
    assert before["autonomy_levels"] == {"fraud-detect": "limited_autonomous"}
    breaches_before = [v for v in verdicts if v["type"] == "quality_breach"]
    assert len(breaches_before) == 1
    verdicts_before = list(verdicts)

    provider_class = await _cycle(module, prov)

    assert await module.get_state() == before
    assert verdicts == verdicts_before
    # The cycle stopped before it built a provider: the early return for an
    # empty response, which keeps a due threshold run from being spent on
    # nothing.
    assert provider_class.call_count == 0

    await _cycle(module, prov)

    after = await module.get_state()
    assert [v for v in verdicts if v["type"] == "quality_breach"] == breaches_before
    assert after["autonomy_levels"] == before["autonomy_levels"]
    assert after["threshold_governed"] == before["threshold_governed"]
