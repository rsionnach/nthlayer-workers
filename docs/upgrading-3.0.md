# Upgrading to nthlayer-workers 3.0

3.0.0 adds judgment-threshold evaluation to the measure module
(opensrm-g32d). It is a major release because manifests that put threshold
fields under `judgment_slo` no longer load, and because it needs new
versions of its neighbours.

## Breaking changes

- **`nthlayer-common` 5.2.** The pin is now `>=5.2.0,<6.0.0`.
- **`nthlayer-core` 3.0 or later.** Thresholds are read from core's
  `/manifests` (the `judgment_thresholds` key). An older core serves none, so
  nothing is evaluated, and threshold state already saved is kept, not
  retired.
- **`calibration`, `segments` and `stability` under `judgment_slo` now fail
  to load.** Move them to `spec.judgment_threshold`. Thresholds are not SLOs:
  they have no target or budget. State saved by 2.x for those removed SLOs is
  left in place and is inert (never counted in governance).

## What is new

- Each threshold is evaluated on its own cadence and emitted as a
  `judgment_threshold_evaluation` assessment (a new kind, alongside
  `judgment_slo_evaluation`), including when it is not evaluable. Reasons:
  `no_data_source` (segments, stability, audit_backlog have no series yet),
  `no_data`, `insufficient_window`, `beyond_top_bucket`.
- A breach emits a `quality_breach` verdict with severity `high`. Like an SLO
  breach, it opens a respond incident (a case in the bench queue, which can
  escalate there) **regardless of `MEASURE_THRESHOLD_AUTONOMY`**: the toggle
  gates only the autonomy ratchet. The verdict carries `breaching_bounds`
  (each breaching bound, or segment, with its measured value and limit) and a
  one-line `summary`, e.g. `calibration threshold breach on svc:
  maximum_expected_calibration_error 0.12 > 0.05`; a breach decided from a
  lower bound reads `>= <limit> (lower bound)`. Respond uses the summary as
  the case briefing. It has no `slo_name`, `current_value` or `target`.
- Threshold state saved by the measure module carries
  `threshold_state_version: 1` (state without it is read as version 1). A
  worker that finds a version it does not know logs
  `threshold_state_version_unknown` and starts threshold state afresh (an
  open breach may re-raise); all other measure state is kept.
- A served entry that fails to parse is logged once and skipped; its
  service's existing threshold state is kept until the manifest parses
  cleanly.

## Metric contract

Series, labels and queries are in the front door's
[metrics-contract.md](https://github.com/rsionnach/nthlayer/blob/main/docs/metrics-contract.md)
("Judgment threshold metrics (opensrm-g32d)"). Two things to act on:

- `gen_ai_feedback_latency_seconds` is now a **histogram** and shares its
  base name with the old gauge. An exporter cannot register both: cut over
  in one release.
- Its buckets need a finite boundary **strictly above every declared bound**.
  OpenTelemetry's default top boundary (10000s) is below a `1d` bound. A
  quantile beyond the top boundary is reported as a lower bound: a breach
  (`lower_bound`) if the boundary is at or above the bound, otherwise not
  evaluable (`beyond_top_bucket`).

## Operator settings

All four are read once at startup: restart the worker to change them.

| Variable | Default | Meaning |
|---|---|---|
| `MEASURE_THRESHOLD_AUTONOMY` | off (`1`/`true`/`yes`/`on` enable) | Threshold breaches feed the autonomy ratchet. |
| `MEASURE_RETENTION_CHECK` | on (`off`/`false`/`0` disable) | Check `prometheus_tsdb_lowest_timestamp_seconds` against the 30d window. |
| `MEASURE_THRESHOLD_INTERVAL` | 900 s | How often thresholds are evaluated, independent of the measure cycle. |
| `MEASURE_THRESHOLD_HEARTBEAT` | 3600 s | An unchanged assessment is re-emitted this often. |

- **Autonomy.** While on, an open breach keeps counting in governance until
  its key re-arms or is removed, not after one `within` evaluation. The
  ratchet is one-way: recovery never raises autonomy. Turning the toggle off
  stops threshold breaches counting; turning it back on re-injects breaches
  that were governed before it went off, without a second `autonomy_change`.
  A breach that opened while it was off does not count (it never had a
  governance decision).
- **Retention check.** Set it **off** behind Thanos sidecar, Mimir or Cortex:
  there the metric describes a local TSDB, not what the query path sees, and
  would wrongly report `insufficient_window`. It allows 1h of slack; an empty
  TSDB counts as unknown retention and measures normally.
- **Interval.** Re-arm after a breach takes 3 intervals measured from the
  first `within` observation AND 3 `within` evaluations. At the default
  cadence a breach re-arms on about the 4th consecutive `within` run (~1h).
- **Heartbeat.** A changed assessment (any bound or segment status or
  reason, or a bound's declared limit) is emitted at once. Dashboards should
  read the latest assessment per key, not count them.
- **Timing values** must be in (0, 604800] (7 days). Anything else (not a
  number, zero, negative, non-finite, larger) is logged and the default used.
