"""Prometheus sampler for judgment thresholds (opensrm-g32d).

Builds queries from nthlayer_common's THRESHOLD_SERIES and maps each result to
the bound it measures. Never decides a bound (the evaluator does) and never
raises: any failure becomes a Missing sample (CORE §5.5.6 E3).

Histogram quantiles are computed here from the bucket counts rather than by
PromQL's histogram_quantile, which reports a quantile in the +Inf bucket as
the largest finite boundary - a value far beyond the top bucket would read as
that boundary and so as within. _quantile reports it as AtLeast(boundary).
"""

from __future__ import annotations

import math
import time as _time
from typing import Any

from nthlayer_common.manifest import (
    FEEDBACK_LATENCY_WINDOW,
    THRESHOLD_SERIES,
    AtLeast,
    JudgmentThreshold,
    Missing,
    Samples,
    ThresholdSeries,
    duration_seconds,
)

# CORE §5.5.5 defines p50/p90 over ground truth that arrived in the last 30
# days, so a young series is measurable. Only a Prometheus whose retention is
# shorter than the window cannot see it: that is insufficient_window. Unknown
# retention (Prometheus not scraping itself) measures what it can.
_RETENTION_QUERY = "min(prometheus_tsdb_lowest_timestamp_seconds)"
# Scrape and block-boundary jitter: a retention configured as exactly 30d must
# not flap to insufficient_window.
_RETENTION_SLACK_SECONDS = 3600.0


def _label(value: str) -> str:
    """A PromQL double-quoted label value: escape backslash, quote, newline."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _selector(labels: dict[str, str]) -> str:
    return ",".join(f'{name}="{_label(value)}"' for name, value in labels.items())


def _result(resp: Any) -> list[Any]:
    if not isinstance(resp, dict):
        return []
    data = resp.get("data")
    result = data.get("result") if isinstance(data, dict) else None
    return result if isinstance(result, list) else []


def _scalar(resp: Any) -> float | None:
    result = _result(resp)
    if not result:
        return None
    try:
        return float(result[0]["value"][1])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _buckets(resp: Any) -> list[tuple[float, float]]:
    """(le, cumulative count) pairs from a `sum by (le)` vector; entries
    without a parseable ``le`` or a finite count are dropped."""
    out = []
    for r in _result(resp):
        try:
            le = float(r["metric"]["le"])  # Prometheus writes the last one "+Inf"
            count = float(r["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if not math.isnan(le) and math.isfinite(count):
            out.append((le, count))
    return out


def _quantile(q: float, buckets: list[tuple[float, float]]) -> float | AtLeast | None:
    """Prometheus's classic-histogram bucketQuantile, except that a rank in
    the +Inf bucket is AtLeast(largest finite le) instead of that le.

    None when there is nothing to measure: no +Inf bucket, no finite bucket,
    or no observations. Duplicate ``le`` values are summed and the counts made
    monotonic, as Prometheus does."""
    if math.isnan(q) or not 0.0 <= q <= 1.0:
        return None
    merged: dict[float, float] = {}
    for le, count in buckets:
        merged[le] = merged.get(le, 0.0) + count
    ordered = sorted(merged.items())
    if len(ordered) < 2 or ordered[-1][0] != math.inf:
        return None
    cumulative: list[tuple[float, float]] = []
    running = 0.0
    for le, count in ordered:
        running = max(running, count)
        cumulative.append((le, running))
    total = cumulative[-1][1]
    if total <= 0:
        return None
    rank = q * total
    b = next(i for i, (_, count) in enumerate(cumulative) if count >= rank)
    if b == len(cumulative) - 1:
        top = cumulative[-2][0]
        # le="-Inf" as the only finite-side bucket bounds nothing.
        return AtLeast(top) if math.isfinite(top) else None
    upper, count = cumulative[b]
    if b == 0 and upper <= 0:
        return upper if math.isfinite(upper) else None
    lower, below = cumulative[b - 1] if b > 0 else (0.0, 0.0)
    if count - below <= 0:  # q=0 over an empty first bucket (Prometheus: NaN)
        return None
    value = lower + (upper - lower) * (rank - below) / (count - below)
    return value if math.isfinite(value) else None  # a -Inf lower bound


async def _query(provider: Any, query: str) -> Any:
    """The response, or Missing("no_data") carrying the error. Never raises."""
    try:
        return await provider.query(query)
    except Exception as exc:  # a sampler never raises (E3)
        return Missing("no_data", f"{type(exc).__name__}: {exc}")


async def fetch_retention(provider: Any) -> float | Missing:
    """Prometheus's oldest-sample timestamp (seconds), or Missing when it is
    unknown. One query; the threshold phase calls it once per cycle."""
    resp = await _query(provider, _RETENTION_QUERY)
    if isinstance(resp, Missing):
        return resp
    v = _scalar(resp)
    if v is None or not math.isfinite(v):
        return Missing("no_data", "retention unknown")
    return v


def needs_retention(threshold: JudgmentThreshold) -> bool:
    """Whether sampling *threshold* checks Prometheus's retention."""
    series = THRESHOLD_SERIES.get(threshold.type, {})
    return any(
        series[b].kind == "histogram_quantile" for b in threshold.bounds if b in series
    )


async def _gauge(provider: Any, s: ThresholdSeries, service: str, t: JudgmentThreshold) -> float | Missing:
    m = t.measurement
    available = {"service": service, "bins": str(m.bins), "window": str(m.window)}
    selector = _selector({label: available[label] for label in s.labels})
    resp = await _query(provider, f"max({s.metric}{{{selector}}})")
    if isinstance(resp, Missing):
        return resp
    v = _scalar(resp)
    if v is None:
        return Missing("no_data", f"no {s.metric} series with {selector}")
    if not math.isfinite(v):
        return Missing("no_data", "non-finite value")
    return v


async def _histogram(
    provider: Any, metric: str, quantiles: dict[str, float], service: str
) -> dict[str, float | AtLeast | Missing]:
    """Every quantile of one histogram, from ONE bucket query."""
    selector = _selector({"service": service})
    resp = await _query(
        provider,
        f"sum by (le) (increase({metric}_bucket{{{selector}}}[{FEEDBACK_LATENCY_WINDOW}]))",
    )
    if isinstance(resp, Missing):
        return dict.fromkeys(quantiles, resp)
    buckets = _buckets(resp)
    if not buckets:
        return dict.fromkeys(quantiles, Missing("no_data", f"no {metric}_bucket series with {selector}"))
    out: dict[str, float | AtLeast | Missing] = {}
    for bound, q in quantiles.items():
        v = _quantile(q, buckets)
        out[bound] = Missing("no_data", "no observations in the window, or no +Inf bucket") if v is None else v
    return out


async def sample(
    provider: Any,
    service: str,
    threshold: JudgmentThreshold,
    *,
    now: float | None = None,
    retention: float | Missing | None = None,
    retention_check: bool = True,
) -> Samples:
    """The raw measurements for *threshold* on *service*. Never raises.

    *retention* is fetch_retention's result, passed in so a cycle queries it
    once; None fetches it here if a bound needs it. retention_check=False
    (MEASURE_RETENTION_CHECK=off) skips the check and measures what
    Prometheus returns."""
    series = THRESHOLD_SERIES.get(threshold.type, {})
    if threshold.type == "segments":
        return Samples(overall=Missing("no_data_source", "no segments series yet"))
    if not series:
        return Samples(bounds={b: Missing("no_data_source", f"no {threshold.type} series yet")
                               for b in threshold.bounds})
    out: dict[str, float | AtLeast | Missing] = {}
    histograms: dict[str, dict[str, float]] = {}  # metric -> {bound: quantile}
    for bound in threshold.bounds:
        s = series.get(bound)
        if s is None:
            out[bound] = Missing("no_data_source", f"no series for {bound}")
        elif s.kind == "gauge":
            out[bound] = await _gauge(provider, s, service, threshold)
        else:
            histograms.setdefault(s.metric, {})[bound] = s.quantile  # type: ignore[assignment]
    if histograms and retention_check:
        if retention is None:
            retention = await fetch_retention(provider)
        now = _time.time() if now is None else now
        window = duration_seconds(FEEDBACK_LATENCY_WINDOW)
        # An empty TSDB reports an int64-max sentinel: later than now, so unknown.
        if not isinstance(retention, Missing) and not (math.isfinite(retention) and retention <= now):
            retention = Missing("no_data", "retention unknown")
        if not isinstance(retention, Missing) and now - retention < window - _RETENTION_SLACK_SECONDS:
            short = Missing("insufficient_window",
                            f"Prometheus retention starts {(now - retention) / 86400:.1f}d ago")
            out.update({b: short for qs in histograms.values() for b in qs})
            histograms = {}
    for metric, quantiles in histograms.items():
        out.update(await _histogram(provider, metric, quantiles, service))
    return Samples(bounds=out)
