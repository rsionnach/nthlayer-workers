"""Observe reads an SLO's query only when its value is a good ratio
[opensrm-1jrk].

The collector computes ``1 - value`` as an error rate. A latency SLO's query
answers in seconds and a custom SLO's in whatever its author chose, so each
is reported as NO_DATA with a ``not_evaluable`` error and its query is never
sent.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
import structlog
from nthlayer_common.gate_models import GateResult
from nthlayer_common.manifest import load_manifest
from nthlayer_common.manifest.models import SLODefinition

from nthlayer_workers.observe.gate.evaluator import check_deploy
from nthlayer_workers.observe.slo.collector import (
    NO_SLO_TYPE,
    NOT_EVALUABLE_PREFIX,
    ServiceSLO,
    SLOMetricCollector,
    not_evaluable_reason,
    results_to_assessments,
)
from nthlayer_workers.observe.store import MemoryAssessmentStore
from nthlayer_workers.observe.worker import _extract_service_slos

# The service and its three SLOs from
# nthlayer-generate/examples/services/search-api.yaml (generate 2010f6d),
# copied here, without its comments and its non-SLO resources, so the suite
# does not read the sibling checkout. Each SLO is
# loaded by nthlayer-common's loader, so the shapes under test are the ones
# the parser produces, not ones written to fit the collector.
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


def _load(tmp_path, text: str = SEARCH_API) -> dict[str, ServiceSLO]:
    path = tmp_path / "search-api.yaml"
    path.write_text(text)
    manifest = load_manifest(path, suppress_deprecation_warning=True)
    return {s.name: ServiceSLO(service=manifest.name, slo=s) for s in manifest.slos}


def _provider(value: float | None):
    provider = AsyncMock()
    provider.get_sli_value = AsyncMock(return_value=value)
    provider.aclose = AsyncMock()
    return provider


async def _collect(items: list[ServiceSLO], value: float | None, collector=None):
    """Collect *items* against a Prometheus that answers *value* to anything."""
    provider = _provider(value)
    collector = collector or SLOMetricCollector("http://prom:9090")
    with patch(
        "nthlayer_workers.observe.slo.collector.PrometheusProvider", return_value=provider
    ):
        results = await collector.collect(items)
    return results, provider


def test_the_fixture_has_the_three_kinds(tmp_path):
    slos = _load(tmp_path)

    assert {name: item.slo.slo_type for name, item in slos.items()} == {
        "search-availability": "availability",
        "search-latency-p95": "latency",
        "search-quality": "custom",
    }
    assert all(item.slo.indicator_query for item in slos.values())


@pytest.mark.parametrize(
    ("slo_name", "slo_type", "value"),
    [
        # A p95 of 0.2 s: read as a ratio it was an SLI of 20%, EXHAUSTED.
        ("search-latency-p95", "latency", 0.2),
        # 97 (a percentage): read as a ratio it was 9700%, HEALTHY.
        ("search-quality", "custom", 97.0),
    ],
)
async def test_a_query_that_is_not_a_good_ratio_is_not_sent(tmp_path, slo_name, slo_type, value):
    item = _load(tmp_path)[slo_name]

    (result,), provider = await _collect([item], value)

    assert result.status == "NO_DATA"
    assert result.error == f"not_evaluable: slo_type '{slo_type}' has no good/total query pair"
    assert (result.current_sli, result.burned_minutes, result.percent_consumed) == (
        None, None, None,
    )
    assert provider.get_sli_value.await_count == 0


async def test_the_reading_carries_no_budget_fields_and_no_share_of_the_aggregate(tmp_path):
    slos = _load(tmp_path)

    results, _ = await _collect(list(slos.values()), 0.999)

    by_name = {r.name: r for r in results}
    assert by_name["search-availability"].status == "HEALTHY"
    (latency,) = [
        a.data for a in results_to_assessments(results, "search-api")
        if a.data["slo_name"] == "search-latency-p95"
    ]
    assert latency == {
        "slo_name": "search-latency-p95",
        "objective": 99.0,
        "window": "30d",
        "total_budget_minutes": pytest.approx(432.0),
        "status": "NO_DATA",
        "error": "not_evaluable: slo_type 'latency' has no good/total query pair",
    }
    summary = SLOMetricCollector("http://prom:9090").calculate_aggregate_budget(results)
    assert summary.valid_slo_count == 1


async def test_an_availability_query_is_sent_with_the_service_substituted(tmp_path):
    item = _load(tmp_path)["search-availability"]

    (result,), provider = await _collect([item], 0.9995)

    assert result.status == "HEALTHY"
    assert result.current_sli == pytest.approx(99.95)
    (query,) = [c.args[0] for c in provider.get_sli_value.await_args_list]
    assert 'service="search-api"' in query and "${service}" not in query


async def test_a_declared_type_is_matched_whatever_its_case(tmp_path):
    """Legacy and srm/v1 pass an explicit ``type:`` through as written."""
    item = _load(tmp_path, SEARCH_API.replace("type: availability", "type: Availability"))[
        "search-availability"
    ]
    assert item.slo.slo_type == "Availability"

    (result,), provider = await _collect([item], 0.9995)

    assert result.status == "HEALTHY"
    assert provider.get_sli_value.await_count == 1


async def test_a_good_total_pair_is_evaluated_whatever_the_type():
    item = ServiceSLO(
        service="svc",
        slo=SLODefinition(
            name="fast-enough", target=99.0, slo_type="latency", window="30d",
            good_query="sum(rate(fast_total[5m]))", total_query="sum(rate(all_total[5m]))",
        ),
    )

    (result,), provider = await _collect([item], 0.995)

    assert result.status != "NO_DATA"
    assert result.current_sli == pytest.approx(99.5)
    (query,) = [c.args[0] for c in provider.get_sli_value.await_args_list]
    assert query == "(sum(rate(fast_total[5m]))) / (sum(rate(all_total[5m])))"


async def test_a_pair_is_sent_in_preference_to_the_slos_own_query():
    """A latency SLO with both: its own query answers in seconds."""
    item = ServiceSLO(
        service="svc",
        slo=SLODefinition(
            name="fast-enough", target=99.0, slo_type="latency", window="30d",
            indicator_query="histogram_quantile(0.95, rate(d_bucket[5m]))",
            good_query="sum(rate(fast_total[5m]))", total_query="sum(rate(all_total[5m]))",
        ),
    )

    (_result,), provider = await _collect([item], 0.995)

    (query,) = [c.args[0] for c in provider.get_sli_value.await_args_list]
    assert query == "(sum(rate(fast_total[5m]))) / (sum(rate(all_total[5m])))"


async def test_an_availability_slo_with_a_blank_pair_member_falls_back_to_its_query():
    item = ServiceSLO(
        service="svc",
        slo=SLODefinition(
            name="availability", target=99.9, slo_type="availability", window="30d",
            indicator_query="some_ratio", good_query="  ", total_query="all_total",
        ),
    )

    (_result,), provider = await _collect([item], 0.9995)

    assert [c.args[0] for c in provider.get_sli_value.await_args_list] == ["some_ratio"]


@pytest.mark.parametrize(
    "queries",
    [
        {"good_query": "sum(rate(fast_total[5m]))"},
        {"total_query": "sum(rate(all_total[5m]))"},
        # A blank member is no member: sent, it would be `(  ) / (...)`.
        {"good_query": "  ", "total_query": "sum(rate(all_total[5m]))"},
        {"good_query": "sum(rate(fast_total[5m]))", "total_query": ""},
    ],
)
async def test_half_a_pair_is_not_a_pair(queries):
    item = ServiceSLO(
        service="svc",
        slo=SLODefinition(
            name="fast-enough", target=99.0, slo_type="latency", window="30d",
            indicator_query="histogram_quantile(0.95, rate(d_bucket[5m]))", **queries,
        ),
    )

    (result,), provider = await _collect([item], 0.995)

    assert result.status == "NO_DATA"
    assert result.error.startswith(NOT_EVALUABLE_PREFIX)
    assert provider.get_sli_value.await_count == 0


@pytest.mark.parametrize("slo_type", ["error_rate", "throughput", "freshness", "bespoke"])
def test_other_kinds_with_a_bare_query_are_not_evaluable(slo_type):
    slo = SLODefinition(
        name="x", target=99.0, slo_type=slo_type, indicator_query="some_series",
    )

    assert not_evaluable_reason(slo) == f"slo_type {slo_type!r} has no good/total query pair"


def test_a_judgment_slo_is_still_evaluable():
    """It loads with slo_type availability: its query is a good ratio."""
    slo = SLODefinition(
        name="reversal_rate", target=98.5, slo_type="availability",
        judgment_type="reversal_rate", indicator_query="1 - (a / b)",
    )

    assert not_evaluable_reason(slo) is None


# ------------------------------------------------------------------ #
# An SLO with no type: core always sends one, so this is a malformed   #
# or older /manifests response                                         #
# ------------------------------------------------------------------ #


def _untyped(slo_type_entry: dict) -> list[ServiceSLO]:
    """An SLO named ``availability`` (so a fallback to the name would read it
    as availability) as the worker builds it from a /manifests body."""
    return _extract_service_slos([{
        "name": "svc",
        "slos": [{
            "name": "availability", "target": 99.9, "window": "30d",
            "indicator_query": "some_ratio", **slo_type_entry,
        }],
    }])


@pytest.mark.parametrize("slo_type_entry", [{}, {"slo_type": None}, {"slo_type": ""}, {"slo_type": "  "}])
async def test_an_slo_without_a_type_is_not_read_as_availability(slo_type_entry):
    items = _untyped(slo_type_entry)
    assert len(items) == 1

    (result,), provider = await _collect(items, 0.5)

    assert result.status == "NO_DATA"
    assert result.error == NOT_EVALUABLE_PREFIX + NO_SLO_TYPE
    assert result.percent_consumed is None
    assert provider.get_sli_value.await_count == 0


async def test_an_untyped_slo_is_warned_about_once_per_service_and_slo():
    collector = SLOMetricCollector("http://prom:9090")
    items = _untyped({}) + [
        dataclasses.replace(_untyped({})[0], service="other-svc"),
    ]

    with structlog.testing.capture_logs() as logs:
        for _cycle in range(3):
            await _collect(items, 0.5, collector)

    warned = [
        (e["service"], e["slo"]) for e in logs if e["event"] == "slo_without_type_not_evaluated"
    ]
    assert sorted(warned) == [("other-svc", "availability"), ("svc", "availability")]
    assert {e["log_level"] for e in logs if e["event"] == "slo_without_type_not_evaluated"} == {
        "warning"
    }


async def test_a_typed_but_not_evaluable_slo_is_not_warned_about(tmp_path):
    item = _load(tmp_path)["search-latency-p95"]

    with structlog.testing.capture_logs() as logs:
        await _collect([item], 0.2)

    assert [e for e in logs if e["event"] == "slo_without_type_not_evaluated"] == []


# ------------------------------------------------------------------ #
# Why a reading and not silence: the gate takes the newest slo_status  #
# per SLO, whatever its age                                            #
# ------------------------------------------------------------------ #


async def test_the_new_reading_supersedes_a_stored_false_exhausted(tmp_path):
    item = _load(tmp_path)["search-latency-p95"]
    store = MemoryAssessmentStore()

    # What the collector stored before: 0.2 s read as an SLI of 20%.
    (old,) = results_to_assessments(
        [dataclasses.replace(
            (await _collect([item], None))[0][0],
            status="EXHAUSTED", error=None, current_sli=20.0,
            burned_minutes=34560.0, percent_consumed=8000.0,
        )],
        "search-api",
    )
    store.put(dataclasses.replace(old, created_at=old.created_at - timedelta(hours=1)))
    assert check_deploy("search-api", "critical", store).result == GateResult.BLOCKED

    (result,), _ = await _collect([item], 0.2)
    for assessment in results_to_assessments([result], "search-api"):
        store.put(assessment)

    gate = check_deploy("search-api", "critical", store)
    assert gate.result == GateResult.APPROVED
    assert gate.budget_remaining_pct == 100.0
    assert gate.slo_count == 1
