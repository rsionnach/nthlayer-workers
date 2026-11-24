"""Observe reads each classical SLO from the error budget generate writes
[opensrm-9qa6].

One series, ``slo:error_budget_remaining:ratio{service, slo}``, read the way
measure reads it. No budget, no reading: an SLO generate writes no budget
for, a judgment SLO, and an SLO with no type are each reported NO_DATA
``not_evaluable`` and never queried, and a missing series is NO_DATA too.
There is no fallback to the manifest's own query.
"""

from __future__ import annotations

import dataclasses
import math
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
import structlog
from nthlayer_common.gate_models import GateResult
from nthlayer_common.manifest import load_manifest
from nthlayer_common.providers.prometheus import PrometheusProviderError

from nthlayer_workers.observe.gate.evaluator import check_deploy
from nthlayer_workers.observe.slo.collector import (
    BUDGET_NOT_FINITE,
    JUDGMENT_SLO,
    NO_BUDGET_SERIES,
    NO_SLO_TYPE,
    NOT_EVALUABLE_PREFIX,
    BudgetReading,
    ServiceSLO,
    SLOMetricCollector,
    results_to_assessments,
)
from nthlayer_workers.observe.store import MemoryAssessmentStore
from nthlayer_workers.observe.worker import _extract_service_slos

# The service and its three SLOs from
# nthlayer-generate/examples/services/search-api.yaml (generate 2010f6d),
# copied here, without its comments and its non-SLO resources, so the suite
# does not read the sibling checkout. Each SLO is loaded by nthlayer-common's
# loader, so the shapes under test are the ones the parser produces.
SEARCH_API = """\
service:
  name: search-api
  team: search
  tier: high
  type: api

resources:
  - kind: SLO
    name: search-availability
    spec:
      objective: 99.5
      window: 30d
      indicator:
        type: availability
        query: |
          sum(rate(http_requests_total{service="${service}",status!~"5.."}[5m]))
          /
          sum(rate(http_requests_total{service="${service}"}[5m]))

  - kind: SLO
    name: search-latency-p95
    spec:
      objective: 99.0
      window: 30d
      indicator:
        type: latency
        percentile: 95
        threshold_ms: 300
        query: |
          histogram_quantile(0.95,
            rate(http_request_duration_seconds_bucket{service="${service}"}[5m])
          )

  - kind: SLO
    name: search-quality
    spec:
      objective: 95.0
      window: 7d
      indicator:
        type: custom
        query: |
          sum(rate(search_results_total{service="${service}",status="found"}[5m]))
          /
          sum(rate(search_results_total{service="${service}"}[5m]))
          * 100
"""

# The service and its first SLO from
# nthlayer-generate/examples/services/checkout-service.yaml (generate
# 2010f6d): the legacy shape that declares an SLO as a good/total pair.
CHECKOUT_SERVICE = """\
apiVersion: nthlayer.dev/v1
kind: Service

service:
  name: checkout-service
  team: commerce
  tier: critical
  type: api

resources:
  - kind: SLO
    name: availability
    spec:
      objective: 99.95
      window: 30d
      indicators:
        - type: availability
          success_ratio:
            total_query: sum(rate(http_requests_total{service="checkout-service"}[5m]))
            good_query: sum(rate(http_requests_total{service="checkout-service",status!~"5.."}[5m]))
"""

# The shape of the shipped nthlayer/demo/specs/fraud-detect.yaml (srm/v1):
# an ai-gate with a judgment SLO beside a classical one.
FRAUD_DETECT = """\
apiVersion: srm/v1
kind: ServiceReliabilityManifest
metadata:
  name: fraud-detect
  team: payments-ml
  tier: critical
spec:
  type: ai-gate
  slos:
    availability:
      target: 99.9
      window: 30d
    reversal_rate:
      target: 98.5
      window: 30d
"""

# The selectors, written out: {service, slo} on the series
# nthlayer/docs/metrics-contract.md names, with no objective matcher.
_SEARCH_AVAILABILITY = (
    'slo:error_budget_remaining:ratio{service="search-api",slo="search-availability"}'
)
_SEARCH_LATENCY = (
    'slo:error_budget_remaining:ratio{service="search-api",slo="search-latency-p95"}'
)


def _load(tmp_path, text: str = SEARCH_API) -> dict[str, ServiceSLO]:
    path = tmp_path / "manifest.yaml"
    path.write_text(text)
    manifest = load_manifest(path, suppress_deprecation_warning=True)
    return {s.name: ServiceSLO(service=manifest.name, slo=s) for s in manifest.slos}


def _sample(remaining, objective: str | None = "99.5", **labels) -> dict:
    """One series of a Prometheus instant-vector result, as the HTTP API
    returns it: a label set and a ``[timestamp, "value"]`` pair. generate
    writes the objective as ``str(target)``, so it arrives as text."""
    metric = {"__name__": "slo:error_budget_remaining:ratio", **labels}
    if objective is not None:
        metric["objective"] = objective
    return {"metric": metric, "value": [1767323045.678, str(remaining)]}


def _vector(*series: dict) -> dict:
    return {"status": "success", "data": {"resultType": "vector", "result": list(series)}}


async def _collect(items: list[ServiceSLO], answers, collector=None):
    """Collect *items* against a Prometheus that answers each query from
    *answers* (query text to response); a query not in it gets an empty
    vector. Returns the results and the queries sent, in order."""
    provider = AsyncMock()
    sent: list[str] = []

    async def query(promql, time=None):
        sent.append(promql)
        answer = answers.get(promql, _vector())
        if isinstance(answer, Exception):
            raise answer
        return answer

    provider.query = query
    provider.aclose = AsyncMock()
    collector = collector or SLOMetricCollector("http://prom:9090")
    with patch(
        "nthlayer_workers.observe.slo.collector.PrometheusProvider", return_value=provider
    ):
        results = await collector.collect(items)
    return results, sent


def _generate_budget(good_ratio: float, objective: float) -> float:
    """generate's rule for the series, as nthlayer/docs/metrics-contract.md
    states it: ``1 - ((1 - good_ratio) / (1 - objective/100))``."""
    return 1 - ((1 - good_ratio) / (1 - objective / 100))


def _budget_fields(result):
    return (result.current_sli, result.burned_minutes, result.percent_consumed)


# ------------------------------------------------------------------ #
# Which SLOs are read, and with which query                            #
# ------------------------------------------------------------------ #


async def test_each_budgeted_kind_is_read_from_its_budget_and_nothing_else_is_sent(tmp_path):
    slos = _load(tmp_path)
    assert {name: item.slo.slo_type for name, item in slos.items()} == {
        "search-availability": "availability",
        "search-latency-p95": "latency",
        "search-quality": "custom",
    }

    results, sent = await _collect(
        list(slos.values()),
        {
            _SEARCH_AVAILABILITY: _vector(_sample(0.6, "99.5")),
            _SEARCH_LATENCY: _vector(_sample(0.9, "99.0")),
        },
    )

    # The manifest's own queries (a 5m ratio, a p95 in seconds, a
    # percentage) are never sent, and the custom SLO is not queried at all.
    assert sent == [_SEARCH_AVAILABILITY, _SEARCH_LATENCY]
    by_name = {r.name: r for r in results}
    assert by_name["search-availability"].status == "HEALTHY"
    assert by_name["search-latency-p95"].status == "HEALTHY"
    assert by_name["search-latency-p95"].percent_consumed == pytest.approx(10.0)
    quality = by_name["search-quality"]
    assert quality.status == "NO_DATA"
    assert quality.error == "not_evaluable: no error budget is written for slo_type 'custom'"
    assert _budget_fields(quality) == (None, None, None)


async def test_a_declared_type_is_matched_whatever_its_case(tmp_path):
    """Legacy and srm/v1 pass an explicit ``type:`` through as written."""
    item = _load(tmp_path, SEARCH_API.replace("type: availability", "type: Availability"))[
        "search-availability"
    ]
    assert item.slo.slo_type == "Availability"

    (result,), sent = await _collect([item], {_SEARCH_AVAILABILITY: _vector(_sample(0.6))})

    assert sent == [_SEARCH_AVAILABILITY]
    assert result.status == "HEALTHY"


async def test_a_good_total_slo_is_read_from_its_budget_not_from_its_pair(tmp_path):
    item = _load(tmp_path, CHECKOUT_SERVICE)["availability"]
    assert item.slo.good_query and item.slo.total_query
    selector = 'slo:error_budget_remaining:ratio{service="checkout-service",slo="availability"}'

    (result,), sent = await _collect([item], {selector: _vector(_sample(0.25, "99.95"))})

    assert sent == [selector]
    assert result.percent_consumed == pytest.approx(75.0)
    assert result.status == "WARNING"


@pytest.mark.parametrize("slo_type", ["throughput", "freshness", "custom", "bespoke"])
async def test_a_kind_with_no_budget_is_not_evaluable_even_with_a_pair(tmp_path, slo_type):
    """generate writes these kinds no budget, so a good/total pair does not
    make one readable."""
    loaded = _load(tmp_path, CHECKOUT_SERVICE)["availability"]
    item = dataclasses.replace(loaded, slo=dataclasses.replace(loaded.slo, slo_type=slo_type))

    (result,), sent = await _collect([item], {})

    assert sent == []
    assert result.status == "NO_DATA"
    assert result.error == (
        f"{NOT_EVALUABLE_PREFIX}no error budget is written for slo_type '{slo_type}'"
    )


async def test_a_judgment_slo_is_left_to_measure(tmp_path):
    slos = _load(tmp_path, FRAUD_DETECT)
    assert slos["reversal_rate"].slo.judgment_type == "reversal_rate"
    # It loads as availability: the kind alone would have it read a budget.
    assert slos["reversal_rate"].slo.slo_type == "availability"
    availability = 'slo:error_budget_remaining:ratio{service="fraud-detect",slo="availability"}'

    results, sent = await _collect(
        list(slos.values()), {availability: _vector(_sample(0.5, "99.9"))}
    )

    assert sent == [availability]
    by_name = {r.name: r for r in results}
    assert by_name["availability"].status == "WARNING"
    assert by_name["reversal_rate"].status == "NO_DATA"
    assert by_name["reversal_rate"].error == NOT_EVALUABLE_PREFIX + JUDGMENT_SLO
    assert _budget_fields(by_name["reversal_rate"]) == (None, None, None)


# ------------------------------------------------------------------ #
# What a budget says                                                   #
# ------------------------------------------------------------------ #


async def test_the_reading_is_computed_from_the_budget_and_its_objective(tmp_path):
    item = _load(tmp_path)["search-availability"]

    (result,), _ = await _collect([item], {_SEARCH_AVAILABILITY: _vector(_sample(0.6, "99.5"))})

    # 30d at 99.5: 43200 min x 0.5% = 216 min of budget, 40% of it used.
    assert result.objective == 99.5
    assert result.total_budget_minutes == pytest.approx(216.0)
    assert result.percent_consumed == pytest.approx(40.0)
    assert result.burned_minutes == pytest.approx(86.4)
    # 40% of a 0.5% budget is 0.2% bad.
    assert result.current_sli == pytest.approx(99.8)
    assert result.status == "HEALTHY"
    assert result.error is None


@pytest.mark.parametrize(
    ("remaining", "percent_consumed", "status"),
    [
        (1.0, 0.0, "HEALTHY"),
        (0.5, 50.0, "WARNING"),
        (0.2, 80.0, "CRITICAL"),
        (0.0, 100.0, "EXHAUSTED"),
        # Signed: an overspent budget is negative.
        (-0.25, 125.0, "EXHAUSTED"),
    ],
)
async def test_status_follows_the_share_of_the_budget_used(
    tmp_path, remaining, percent_consumed, status
):
    item = _load(tmp_path)["search-availability"]

    (result,), _ = await _collect(
        [item], {_SEARCH_AVAILABILITY: _vector(_sample(remaining, "99.5"))}
    )

    assert result.percent_consumed == pytest.approx(percent_consumed)
    assert result.status == status


@pytest.mark.parametrize("objective", [90.0, 99.0, 99.5, 99.95])
@pytest.mark.parametrize("good_ratio", [1.0, 0.9999, 0.995, 0.97, 0.5])
def test_the_sli_is_generates_formula_solved_for_the_good_ratio(good_ratio, objective):
    reading = BudgetReading(remaining=_generate_budget(good_ratio, objective), objective=objective)

    assert reading.sli_percent == pytest.approx(good_ratio * 100, abs=1e-9)


async def test_the_round_trip_holds_through_the_collector(tmp_path):
    """A good ratio of 99.2% against 99.0: generate's budget for it comes
    back as an SLI of 99.2 and 80% of the budget used."""
    item = _load(tmp_path)["search-latency-p95"]
    budget = _generate_budget(0.992, 99.0)

    (result,), _ = await _collect([item], {_SEARCH_LATENCY: _vector(_sample(budget, "99.0"))})

    assert result.current_sli == pytest.approx(99.2)
    assert result.percent_consumed == pytest.approx(80.0)
    assert result.burned_minutes == pytest.approx(43200 * 0.01 * 0.8)


# ------------------------------------------------------------------ #
# The objective is the series' own                                     #
# ------------------------------------------------------------------ #


async def test_the_series_objective_is_used_and_reported_not_the_manifests(tmp_path):
    """The manifest says 99.5; the rules in Prometheus were generated at 99.0."""
    item = _load(tmp_path)["search-availability"]
    assert item.slo.target == 99.5

    with structlog.testing.capture_logs() as logs:
        (result,), _ = await _collect(
            [item], {_SEARCH_AVAILABILITY: _vector(_sample(0.6, "99.0"))}
        )

    assert result.objective == 99.0
    assert result.total_budget_minutes == pytest.approx(432.0)
    assert result.burned_minutes == pytest.approx(172.8)
    assert result.current_sli == pytest.approx(99.6)
    (data,) = [a.data for a in results_to_assessments([result], "search-api")]
    assert data["objective"] == 99.0
    (warning,) = [e for e in logs if e["event"] == "slo_objective_differs_from_manifest"]
    assert warning["log_level"] == "warning"
    assert (warning["service"], warning["slo"]) == ("search-api", "search-availability")
    assert (warning["manifest_target"], warning["budget_objective"]) == (99.5, 99.0)


async def test_a_differing_objective_is_warned_about_once_per_service_and_slo(tmp_path):
    slos = _load(tmp_path)
    items = [slos["search-availability"], slos["search-latency-p95"]]
    collector = SLOMetricCollector("http://prom:9090")
    answers = {
        _SEARCH_AVAILABILITY: _vector(_sample(0.6, "99.0")),
        _SEARCH_LATENCY: _vector(_sample(0.6, "98.0")),
    }

    with structlog.testing.capture_logs() as logs:
        for _cycle in range(3):
            await _collect(items, answers, collector)

    warned = [e["slo"] for e in logs if e["event"] == "slo_objective_differs_from_manifest"]
    assert sorted(warned) == ["search-availability", "search-latency-p95"]


@pytest.mark.parametrize("label", ["99.5", "99.50"])
async def test_an_objective_equal_to_the_manifests_is_not_warned_about(tmp_path, label):
    item = _load(tmp_path)["search-availability"]

    with structlog.testing.capture_logs() as logs:
        (result,), _ = await _collect(
            [item], {_SEARCH_AVAILABILITY: _vector(_sample(0.6, label))}
        )

    assert result.objective == 99.5
    assert [e for e in logs if e["event"] == "slo_objective_differs_from_manifest"] == []


# ------------------------------------------------------------------ #
# No budget to read                                                    #
# ------------------------------------------------------------------ #


async def test_no_series_is_no_data_with_no_fallback_to_the_manifest_query(tmp_path):
    item = _load(tmp_path)["search-availability"]
    assert item.slo.indicator_query

    (result,), sent = await _collect([item], {})

    assert sent == [_SEARCH_AVAILABILITY]
    assert result.status == "NO_DATA"
    assert result.error == NO_BUDGET_SERIES
    assert _budget_fields(result) == (None, None, None)
    # Nothing was read, so the manifest's target stands.
    assert result.objective == 99.5


@pytest.mark.parametrize("value", ["NaN", "+Inf", "-Inf"])
async def test_a_budget_that_is_not_a_number_is_no_data(tmp_path, value):
    """The rule divides by the request count: with no requests it is NaN."""
    item = _load(tmp_path)["search-availability"]

    with structlog.testing.capture_logs() as logs:
        (result,), _ = await _collect(
            [item], {_SEARCH_AVAILABILITY: _vector(_sample(value, "99.5"))}
        )

    assert result.status == "NO_DATA"
    assert result.error == BUDGET_NOT_FINITE
    assert _budget_fields(result) == (None, None, None)
    assert [e for e in logs if e["event"] == "error_budget_unreadable"] == []


@pytest.mark.parametrize(
    ("series", "error"),
    [
        (
            [_sample(0.6, "99.5"), _sample(0.1, "99.9")],
            "2 error budget series for one SLO",
        ),
        ([_sample(0.6, None)], "error budget series has no objective label"),
        ([_sample(0.6, "high")], "error budget series has no objective label"),
        (
            [_sample(0.6, "100.0")],
            "error budget series has objective 100.0, which leaves no budget",
        ),
        (
            [_sample(0.6, "0.0")],
            "error budget series has objective 0.0, which leaves no budget",
        ),
        ([{"metric": {"objective": "99.5"}, "value": [1767323045.678]}],
         "error budget sample has no value"),
        ([{"metric": {"objective": "99.5"}}], "error budget sample has no value"),
    ],
)
async def test_a_budget_that_cannot_be_read_is_an_error_with_no_budget_fields(
    tmp_path, series, error
):
    item = _load(tmp_path)["search-availability"]

    with structlog.testing.capture_logs() as logs:
        (result,), _ = await _collect([item], {_SEARCH_AVAILABILITY: _vector(*series)})

    assert result.status == "ERROR"
    assert result.error == error
    assert _budget_fields(result) == (None, None, None)
    (logged,) = [e for e in logs if e["event"] == "error_budget_unreadable"]
    assert (logged["service"], logged["slo"], logged["reason"]) == (
        "search-api", "search-availability", error,
    )


async def test_a_failed_query_is_an_error_and_the_next_slo_is_still_read(tmp_path):
    slos = _load(tmp_path)

    results, sent = await _collect(
        [slos["search-availability"], slos["search-latency-p95"]],
        {
            _SEARCH_AVAILABILITY: PrometheusProviderError("timeout"),
            _SEARCH_LATENCY: _vector(_sample(0.9, "99.0")),
        },
    )

    assert sent == [_SEARCH_AVAILABILITY, _SEARCH_LATENCY]
    assert (results[0].status, results[0].error) == ("ERROR", "timeout")
    assert _budget_fields(results[0]) == (None, None, None)
    assert results[1].status == "HEALTHY"


async def test_readings_without_a_budget_take_no_share_of_the_aggregate(tmp_path):
    slos = _load(tmp_path)

    results, _ = await _collect(
        list(slos.values()), {_SEARCH_AVAILABILITY: _vector(_sample(0.6, "99.5"))}
    )

    summary = SLOMetricCollector("http://prom:9090").calculate_aggregate_budget(results)
    assert summary.valid_slo_count == 1
    assert summary.total_budget_minutes == pytest.approx(216.0)
    assert summary.consumed_percent == pytest.approx(40.0)


# ------------------------------------------------------------------ #
# An SLO with no type: core always sends one, so this is a malformed   #
# or older /manifests response                                         #
# ------------------------------------------------------------------ #


def _untyped(slo_type_entry: dict) -> list[ServiceSLO]:
    """An SLO named ``availability`` (so a fallback to the name would read it
    as availability) as the worker builds it from a /manifests body."""
    return _extract_service_slos([{
        "name": "svc",
        "slos": [{"name": "availability", "target": 99.9, "window": "30d", **slo_type_entry}],
    }])


@pytest.mark.parametrize(
    "slo_type_entry", [{}, {"slo_type": None}, {"slo_type": ""}, {"slo_type": "  "}]
)
async def test_an_slo_without_a_type_is_not_read_as_availability(slo_type_entry):
    items = _untyped(slo_type_entry)
    assert len(items) == 1

    (result,), sent = await _collect(items, {})

    assert sent == []
    assert result.status == "NO_DATA"
    assert result.error == NOT_EVALUABLE_PREFIX + NO_SLO_TYPE
    assert result.percent_consumed is None


async def test_a_typed_slo_from_the_worker_path_is_read_from_its_budget():
    (item,) = _untyped({"slo_type": "availability"})
    selector = 'slo:error_budget_remaining:ratio{service="svc",slo="availability"}'

    (result,), sent = await _collect([item], {selector: _vector(_sample(0.6, "99.9"))})

    assert sent == [selector]
    assert result.percent_consumed == pytest.approx(40.0)


async def test_an_untyped_slo_is_warned_about_once_per_service_and_slo():
    collector = SLOMetricCollector("http://prom:9090")
    items = _untyped({}) + [dataclasses.replace(_untyped({})[0], service="other-svc")]

    with structlog.testing.capture_logs() as logs:
        for _cycle in range(3):
            await _collect(items, {}, collector)

    events = [e for e in logs if e["event"] == "slo_without_type_not_evaluated"]
    assert sorted((e["service"], e["slo"]) for e in events) == [
        ("other-svc", "availability"), ("svc", "availability"),
    ]
    assert {e["log_level"] for e in events} == {"warning"}


async def test_a_typed_but_not_evaluable_slo_is_not_warned_about(tmp_path):
    item = _load(tmp_path)["search-quality"]

    with structlog.testing.capture_logs() as logs:
        await _collect([item], {})

    assert [e for e in logs if e["event"] == "slo_without_type_not_evaluated"] == []


# ------------------------------------------------------------------ #
# The gate, with readings that state no budget                         #
# ------------------------------------------------------------------ #


async def test_a_service_with_no_budget_series_gets_a_warning_gate(tmp_path):
    """generate's rules are not loaded: every classical SLO reads NO_DATA.
    That supersedes an older reading, and it is not a healthy budget."""
    slos = _load(tmp_path)
    store = MemoryAssessmentStore()

    # An older reading for one SLO, from when it was still being read.
    (old_result,), _ = await _collect(
        [slos["search-availability"]], {_SEARCH_AVAILABILITY: _vector(_sample(-0.5, "99.5"))}
    )
    assert old_result.status == "EXHAUSTED"
    (old,) = results_to_assessments([old_result], "search-api")
    store.put(dataclasses.replace(old, created_at=old.created_at - timedelta(hours=1)))
    assert check_deploy("search-api", "critical", store).result == GateResult.BLOCKED

    results, _ = await _collect(list(slos.values()), {})
    for assessment in results_to_assessments(results, "search-api"):
        store.put(assessment)

    gate = check_deploy("search-api", "critical", store)
    assert gate.result == GateResult.WARNING
    assert gate.budget_remaining_pct is None
    assert gate.slo_count == 3
    assert gate.message == "No budget data for 3 SLOs"


def test_a_budget_reading_is_frozen():
    reading = BudgetReading(remaining=0.6, objective=99.5)

    with pytest.raises(dataclasses.FrozenInstanceError):
        reading.remaining = 0.1
    assert math.isclose(reading.consumed, 0.4)
