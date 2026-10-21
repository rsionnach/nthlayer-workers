"""Threshold sampler: Prometheus responses -> Samples (opensrm-g32d).

These tests feed realistic Prometheus response JSON and assert which bound
each value lands on - not just the query string (the gap that hid g32d)."""

import math
from unittest.mock import AsyncMock

import pytest
from nthlayer_common.manifest import (
    AtLeast,
    JudgmentThreshold,
    Missing,
    ThresholdMeasurement,
    evaluate_threshold,
)
from nthlayer_common.manifest.models import ProbeConfig

from nthlayer_workers.measure.thresholds.sampler import _quantile, fetch_retention, sample

NOW = 1_800_000_000.0
DAY = 86400.0
INF = math.inf


def _vector(value):
    return {"status": "success", "data": {"resultType": "vector",
            "result": [{"metric": {}, "value": [NOW, str(value)]}]}}


def _buckets(*pairs):
    """A `sum by (le) (increase(..._bucket[30d]))` response: one sample per
    bucket, cumulative counts, `le` formatted as Prometheus writes it."""
    return {"status": "success", "data": {"resultType": "vector", "result": [
        {"metric": {"le": le}, "value": [NOW, str(count)]} for le, count in pairs]}}


_EMPTY = {"status": "success", "data": {"resultType": "vector", "result": []}}

# A needle for _provider that matches every query ("" is a substring of any
# string): use it to make every query answer the same way.
_ANY = ""
# The feedback-latency query reads the histogram's buckets.
_BUCKETS = "gen_ai_feedback_latency_seconds_bucket"


def _provider(responses):
    """responses: list of (substring, response) — the first whose substring
    occurs in the query answers it."""
    async def query(q, time=None):
        for needle, resp in responses:
            if needle in q:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        return _EMPTY
    p = AsyncMock()
    p.query = AsyncMock(side_effect=query)
    return p


def _queries(p):
    return [c.args[0] for c in p.query.await_args_list]


def _cal(**bounds):
    return JudgmentThreshold("calibration", dict(bounds), ThresholdMeasurement(bins=10, window="30d"))


async def test_ece_and_brier_land_on_their_own_bounds():
    p = _provider([("gen_ai_calibration_ece", _vector(0.2)), ("gen_ai_calibration_brier", _vector(0.03))])
    s = await sample(p, "svc", _cal(maximum_expected_calibration_error=0.05, maximum_brier_score=0.1), now=NOW)
    assert s.bounds == {"maximum_expected_calibration_error": 0.2, "maximum_brier_score": 0.03}


async def test_calibration_query_selects_declared_bins_and_window():
    p = _provider([])
    await sample(p, "svc", _cal(maximum_brier_score=0.1), now=NOW)
    q = p.query.await_args.args[0]
    assert 'service="svc"' in q and 'bins="10"' in q and 'window="30d"' in q


async def test_gauge_query_takes_the_max_across_series():
    # One series per service/bins/window is the contract; max() is the
    # conservative fallback when an exporter sends several (e.g. per instance).
    p = _provider([])
    await sample(p, "svc", _cal(maximum_expected_calibration_error=0.05, maximum_brier_score=0.1), now=NOW)
    assert [q.split("{")[0] for q in _queries(p)] == [
        "max(gen_ai_calibration_ece", "max(gen_ai_calibration_brier"]


async def test_mismatched_bins_label_is_no_data():
    # Prometheus has a series only for bins=15; the label matcher for bins=10
    # returns an empty vector.
    p = _provider([('bins="15"', _vector(0.9))])
    s = await sample(p, "svc", _cal(maximum_brier_score=0.1), now=NOW)
    v = s.bounds["maximum_brier_score"]
    assert isinstance(v, Missing) and v.reason == "no_data"
    assert 'bins="10"' in v.detail and 'window="30d"' in v.detail  # names what it looked for


async def test_label_values_are_escaped():
    p = _provider([])
    await sample(p, 'we"ird\\svc', _cal(maximum_brier_score=0.1), now=NOW)
    assert 'service="we\\"ird\\\\svc"' in p.query.await_args.args[0]


def _lat(p50="1h", p90="1d"):
    return JudgmentThreshold("feedback_latency", {"maximum_p50": p50, "maximum_p90": p90})


def _retention(start):
    # Prometheus's own oldest-sample timestamp (seconds).
    return ("prometheus_tsdb_lowest_timestamp_seconds", _vector(start))


_OLD = _retention(NOW - 31 * DAY)


# Prometheus's bucketQuantile, worked by hand (rank = q * total; first bucket
# whose cumulative count >= rank; linear between its lower and upper le).
@pytest.mark.parametrize("q,buckets,expected", [
    # total 10. q.5: rank 5 -> le=10 (count 5), lower 0: 0 + 10 * 5/5 = 10.
    (0.5, [(10, 5), (100, 10), (INF, 10)], 10.0),
    # q.9: rank 9 -> le=100: 10 + (100 - 10) * (9 - 5)/(10 - 5) = 82.
    (0.9, [(10, 5), (100, 10), (INF, 10)], 82.0),
    # total 100. q.5: rank 50 -> le=86400: 3600 + 82800 * (50 - 40)/(90 - 40) = 20160.
    (0.5, [(3600, 40), (86400, 90), (INF, 100)], 20160.0),
    # q.9: rank 90 lands exactly on le=86400's count: 3600 + 82800 * 50/50 = 86400.
    (0.9, [(3600, 40), (86400, 90), (INF, 100)], 86400.0),
    # rank 50 > 20 = every finite count: the +Inf bucket -> a lower bound.
    (0.5, [(3600, 10), (86400, 20), (INF, 100)], AtLeast(86400.0)),
    # Unsorted input, and a duplicate le (10 and 10.0) summed, as Prometheus does.
    (0.5, [(INF, 10), (100, 10), (10, 2), (10.0, 3)], 10.0),
    # Non-monotonic counts are repaired (le=100 -> 5): rank 9 is past every finite bucket.
    (0.9, [(10, 5), (100, 4), (INF, 10)], AtLeast(100.0)),
    # Nothing to measure: no observations, no +Inf bucket, no finite bucket.
    (0.5, [(10, 0), (INF, 0)], None),
    (0.5, [(10, 5), (100, 10)], None),
    (0.5, [(INF, 5)], None),
    # q=0 with an empty first bucket: Prometheus divides 0/0 (NaN), not a value.
    (0.0, [(10, 0), (100, 5), (INF, 5)], None),
    # le="-Inf" is the largest finite-side boundary: no finite lower bound exists.
    (0.5, [(-INF, 1), (INF, 10)], None),
])
def test_quantile_is_prometheus_bucket_quantile(q, buckets, expected):
    assert _quantile(q, buckets) == expected


async def test_latency_quantiles_land_on_p50_and_p90():
    # total 100: p50 rank 50 -> 3600 + 82800 * (50 - 40)/50 = 20160;
    # p90 rank 90 -> exactly 86400.
    p = _provider([_OLD, (_BUCKETS, _buckets(("3600", 40), ("86400", 90), ("+Inf", 100)))])
    s = await sample(p, "svc", _lat(), now=NOW)
    assert s.bounds == {"maximum_p50": 20160.0, "maximum_p90": 86400.0}


async def test_one_bucket_query_serves_both_quantiles():
    p = _provider([_OLD, (_BUCKETS, _buckets(("10", 5), ("+Inf", 10)))])
    await sample(p, "svc", _lat(), now=NOW)
    bucket_queries = [q for q in _queries(p) if _BUCKETS in q]
    assert bucket_queries == [
        'sum by (le) (increase(gen_ai_feedback_latency_seconds_bucket{service="svc"}[30d]))'
    ]
    assert not any("histogram_quantile" in q for q in _queries(p))


async def test_quantile_beyond_the_top_bucket_is_a_lower_bound():
    # Most ground truth arrives after more than a day: the rank falls in +Inf.
    p = _provider([_OLD, (_BUCKETS, _buckets(("3600", 5), ("86400", 10), ("+Inf", 100)))])
    s = await sample(p, "svc", _lat(), now=NOW)
    assert s.bounds == {"maximum_p50": AtLeast(86400.0), "maximum_p90": AtLeast(86400.0)}


async def test_latency_beyond_a_1d_top_bucket_breaches_a_1d_bound():
    """End to end through evaluate_threshold: histogram_quantile would report
    86400 here, equal to the bound and so within. The true p90 is above it."""
    p = _provider([_OLD, (_BUCKETS, _buckets(("60", 1), ("3600", 5), ("86400", 10), ("+Inf", 100)))])
    t = _lat(p50="1d", p90="1d")
    o = evaluate_threshold(t, await sample(p, "svc", t, now=NOW))
    assert o.status == "breach"
    assert [(b.status, b.measured, b.reason) for b in o.bounds] == [
        ("breach", 86400.0, "lower_bound"), ("breach", 86400.0, "lower_bound")]


async def test_latency_beyond_a_top_bucket_below_the_bound_is_not_evaluable():
    # OTel's default top boundary is 10000s (< 1d): "more than 10000s" says
    # nothing about a 1d bound.
    p = _provider([_OLD, (_BUCKETS, _buckets(("5000", 1), ("10000", 2), ("+Inf", 10)))])
    t = _lat(p50="1d", p90="1d")
    o = evaluate_threshold(t, await sample(p, "svc", t, now=NOW))
    assert [(b.status, b.reason) for b in o.bounds] == [("not_evaluable", "beyond_top_bucket")] * 2


@pytest.mark.parametrize("response", [
    _buckets(("10", 0), ("+Inf", 0)),       # no ground truth arrived in 30d
    _buckets(("10", 5), ("100", 10)),       # no +Inf bucket: not a histogram
    _EMPTY,                                 # no series at all
])
async def test_unmeasurable_histogram_is_no_data(response):
    p = _provider([_OLD, (_BUCKETS, response)])
    s = await sample(p, "svc", _lat(), now=NOW)
    assert all(isinstance(v, Missing) and v.reason == "no_data" for v in s.bounds.values())


async def test_no_bucket_series_names_the_selector():
    p = _provider([_OLD])
    s = await sample(p, "svc", _lat(), now=NOW)
    assert 'service="svc"' in s.bounds["maximum_p50"].detail


async def test_short_retention_is_insufficient_window():
    # Prometheus keeps only 3 days: it cannot see the 30-day window.
    p = _provider([_retention(NOW - 3 * DAY), (_BUCKETS, _buckets(("10", 5), ("+Inf", 10)))])
    s = await sample(p, "svc", _lat(), now=NOW)
    assert {v.reason for v in s.bounds.values()} == {"insufficient_window"}
    assert not any(_BUCKETS in q for q in _queries(p))


async def test_young_series_is_measured():
    # CORE §5.5.5: p50/p90 over ground truth that ARRIVED in the last 30 days.
    # A service that started 3 days ago has exactly that quantity; only a
    # Prometheus that cannot see 30 days back makes it unmeasurable.
    p = _provider([_retention(NOW - 90 * DAY), (_BUCKETS, _buckets(("10", 5), ("100", 10), ("+Inf", 10)))])
    s = await sample(p, "svc", _lat(), now=NOW)
    assert s.bounds == {"maximum_p50": 10.0, "maximum_p90": 82.0}


async def test_unknown_retention_still_measures():
    # prometheus_tsdb_lowest_timestamp_seconds not scraped: retention unknown,
    # so measure what Prometheus returns rather than hide the bound.
    p = _provider([(_BUCKETS, _buckets(("10", 5), ("100", 10), ("+Inf", 10)))])
    s = await sample(p, "svc", _lat(), now=NOW)
    assert s.bounds == {"maximum_p50": 10.0, "maximum_p90": 82.0}


async def test_retention_check_off_sends_no_retention_query():
    # MEASURE_RETENTION_CHECK=off (Thanos sidecar / Mimir / Cortex, where the
    # metric does not describe what the query path can see).
    p = _provider([_retention(NOW - 3 * DAY), (_BUCKETS, _buckets(("10", 5), ("100", 10), ("+Inf", 10)))])
    s = await sample(p, "svc", _lat(), now=NOW, retention_check=False)
    assert s.bounds == {"maximum_p50": 10.0, "maximum_p90": 82.0}
    assert not any("prometheus_tsdb" in q for q in _queries(p))


async def test_prefetched_retention_is_used_not_requeried():
    p = _provider([_retention(NOW - 90 * DAY)])
    short = await fetch_retention(_provider([_retention(NOW - 3 * DAY)]))
    s = await sample(p, "svc", _lat(), now=NOW, retention=short)
    assert {v.reason for v in s.bounds.values()} == {"insufficient_window"}
    assert not any("prometheus_tsdb" in q for q in _queries(p))


async def test_prometheus_error_is_no_data_and_does_not_raise():
    p = _provider([(_ANY, RuntimeError("connection refused"))])
    s = await sample(p, "svc", _cal(maximum_brier_score=0.1), now=NOW)
    v = s.bounds["maximum_brier_score"]
    assert v.reason == "no_data" and "connection refused" in v.detail


async def test_nan_gauge_is_no_data():
    p = _provider([(_ANY, _vector("NaN"))])
    s = await sample(p, "svc", _cal(maximum_brier_score=0.1), now=NOW)
    assert s.bounds["maximum_brier_score"].reason == "no_data"


@pytest.mark.parametrize("t,bounds,measurement", [
    ("segments", {"maximum_variance_from_overall": 0.05},
     ThresholdMeasurement(metric="escalation", segment_dimension="region", segment_values=["eu"], window="30d")),
    ("stability", {"maximum_drift": 0.02},
     ThresholdMeasurement(probe=ProbeConfig("s3://p", "daily"))),
    ("audit_backlog", {"maximum_age": "24h"}, ThresholdMeasurement()),
])
async def test_unsourced_types_are_no_data_source(t, bounds, measurement):
    p = _provider([])
    s = await sample(p, "svc", JudgmentThreshold(t, bounds, measurement), now=NOW)
    if t == "segments":
        assert s.overall.reason == "no_data_source"
    else:
        assert {v.reason for v in s.bounds.values()} == {"no_data_source"}
    p.query.assert_not_awaited()


@pytest.mark.parametrize("q", [1.5, -0.1, math.nan])
def test_quantile_out_of_range_is_none(q):
    assert _quantile(q, [(10, 5), (INF, 10)]) is None


@pytest.mark.parametrize("buckets", [
    [(-INF, 5), (INF, 10)],                 # first bucket le=-Inf, rank inside it
    [(-INF, 1), (10, 5), (INF, 10)],        # interpolating from a -Inf lower bound
])
def test_quantile_never_returns_non_finite(buckets):
    for q in (0.05, 0.5, 0.9):
        v = _quantile(q, buckets)
        assert v is None or math.isfinite(v if not isinstance(v, AtLeast) else v.value)


def test_quantile_neg_inf_lower_bound_is_none():
    assert _quantile(0.5, [(-INF, 1), (10, 5), (INF, 10)]) is None
    assert _quantile(0.05, [(-INF, 5), (INF, 10)]) is None


@pytest.mark.parametrize("retention", [9.223372036854776e15, NOW + 1, math.inf])
async def test_future_or_sentinel_retention_is_unknown_and_measures(retention):
    p = _provider([_retention(retention), (_BUCKETS, _buckets(("10", 5), ("100", 10), ("+Inf", 10)))])
    s = await sample(p, "svc", _lat(), now=NOW)
    assert s.bounds == {"maximum_p50": 10.0, "maximum_p90": 82.0}


@pytest.mark.parametrize("resp", [None, "garbage", [], {"data": {"result": "x"}}, {"data": None}])
async def test_malformed_gauge_response_is_no_data(resp):
    p = _provider([(_ANY, resp)])
    s = await sample(p, "svc", _cal(maximum_brier_score=0.1), now=NOW)
    assert s.bounds["maximum_brier_score"].reason == "no_data"


@pytest.mark.parametrize("resp", [None, "garbage", {"data": {"result": "x"}}])
async def test_malformed_histogram_response_is_no_data(resp):
    p = _provider([_OLD, (_BUCKETS, resp)])
    s = await sample(p, "svc", _lat(), now=NOW)
    assert all(v.reason == "no_data" for v in s.bounds.values())


async def test_bucket_with_unparseable_le_is_ignored_not_fatal():
    p = _provider([_OLD, (_BUCKETS, _buckets(("abc", 99), ("10", 5), ("100", 10), ("+Inf", 10)))])
    s = await sample(p, "svc", _lat(), now=NOW)
    assert s.bounds == {"maximum_p50": 10.0, "maximum_p90": 82.0}


@pytest.mark.parametrize("value", ["+Inf", "-Inf"])
async def test_infinite_gauge_is_no_data(value):
    p = _provider([(_ANY, _vector(value))])
    s = await sample(p, "svc", _cal(maximum_brier_score=0.1), now=NOW)
    assert s.bounds["maximum_brier_score"].reason == "no_data"


# Prometheus promql/quantile.go bucketQuantile, worked by hand for the cases
# the table above does not reach.
@pytest.mark.parametrize("q,buckets,expected", [
    # A rank in the first bucket interpolates from 0 when its le is above 0
    # (bucketQuantile: "if b == 0 ... bucketStart = 0"): 0 + 10 * 2/4 = 5.
    (0.5, [(10, 4), (INF, 4)], 5.0),
    # A first bucket with le <= 0 returns its upper bound, no interpolation.
    (0.5, [(-1, 4), (INF, 4)], -1.0),
    (0.5, [(0, 4), (INF, 4)], 0.0),
    # Non-monotonic counts are repaired to the running maximum before the
    # rank is located: le=2 reads 2, not 1. rank 3 -> le=4:
    # 2 + (4 - 2) * (3 - 2)/(4 - 2) = 3 (unrepaired it would be 2 + 2 * 2/3).
    (0.75, [(1, 2), (2, 1), (4, 4), (INF, 4)], 3.0),
])
def test_quantile_first_bucket_and_monotonic_repair(q, buckets, expected):
    assert _quantile(q, buckets) == expected


_SLACK = 3600.0                  # metrics-contract.md: "one hour of slack"
_WINDOW = 30 * DAY


@pytest.mark.parametrize("age,measured", [
    (_WINDOW, True),                     # retention configured as exactly 30d
    (_WINDOW - _SLACK, True),            # at the edge of the slack
    (_WINDOW - _SLACK - 1.0, False),     # just beyond it
    (_WINDOW - 2 * _SLACK, False),
])
async def test_retention_check_allows_one_hour_of_slack(age, measured):
    p = _provider([_retention(NOW - age), (_BUCKETS, _buckets(("10", 5), ("100", 10), ("+Inf", 10)))])
    s = await sample(p, "svc", _lat(), now=NOW)
    if measured:
        assert s.bounds == {"maximum_p50": 10.0, "maximum_p90": 82.0}
    else:
        assert {v.reason for v in s.bounds.values()} == {"insufficient_window"}


async def test_newline_in_a_label_value_is_escaped():
    # PromQL string literals: a raw newline ends the selector; "\n" is the escape.
    p = _provider([])
    await sample(p, "a\nb", _cal(maximum_brier_score=0.1), now=NOW)
    q = p.query.await_args.args[0]
    assert 'service="a\\nb"' in q and "\n" not in q
