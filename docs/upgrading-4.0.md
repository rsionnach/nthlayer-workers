# Upgrading to nthlayer-workers 4.0

4.0.0 brings the telemetry workers emits into line with OpenSRM Addendum A1
(opensrm-ju8r). It is a major release because every OTel event the measure
module emits is renamed, and their attribute keys change. Dashboards, alerts
or trace queries keyed on the old names stop matching. Nothing in the
NthLayer ecosystem reads them; the change is visible only to your own
observability setup.

A1 §4.0 bars `gen_ai.*` names for anything OpenSRM or NthLayer defines. The
measure module's scores are grader evaluations, so they now use the upstream
OpenTelemetry grader event, `gen_ai.evaluation.result` (A1 §4.4). Its other
events become `nthlayer.*` implementation events.

In 4.0 the events were span events on the current span. Nothing in workers
made a span recording, so none of them was ever exported. From 4.1 they are
OTel log records, as A1 §4.0 describes (opensrm-quqo): each record's
`event_name` is the event name, its attributes are the keys below, and its
Timestamp is when the event happened. The `nthlayer.llm.call` events the
respond agents and correlate's summary emit start exporting the same way. A
deployment that ran its own recording TracerProvider and read these as span
events must read them as logs instead.

To export them, set an OTLP endpoint (`OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` or
`OTEL_EXPORTER_OTLP_ENDPOINT`). The exporter is gRPC only, so point it at a
collector's gRPC port (4317, not 4318); `OTEL_EXPORTER_OTLP_PROTOCOL` is not
read. Every workers CLI calls nthlayer-common's `init_telemetry` at
startup. All of them report `service.name` `nthlayer-workers`; set
`OTEL_SERVICE_NAME` per process to tell them apart. The SDK and gRPC exporter
(`nthlayer-common[otel]`, which pulls in grpcio and protobuf) are now
installed with workers. Query the events as logs, not spans. A one-shot CLI
command flushes its events as it exits, and with the collector unreachable it
waits up to `OTEL_EXPORTER_OTLP_TIMEOUT` (10s by default) to do so; set it
lower where that matters, e.g. in scripts.

## 4.2: every classical SLO is evaluated from its error budget

From 4.2 (opensrm-zo0o, ruling Z1) `nthlayer-measure` evaluates every
classical SLO from `slo:error_budget_remaining:ratio{service, slo}`, the
series nthlayer-generate writes, and breaches when it goes negative. Before,
only availability SLOs read it; a latency SLO named `latency` compared a
percentile in seconds with its target, and every other classical SLO read
`slo:<name>:ratio`, which nothing wrote for error_rate, throughput, freshness,
lag or custom, so they were never evaluated.

**Deploy nthlayer-generate 4.1.0's recording rules first.** Earlier
generate releases write a budget for availability SLOs only, so with them a
latency or error_rate SLO now reads no data and is not evaluated, where
workers 4.1 evaluated a latency SLO from its percentile. Budgets also read low for their
first window after the rules are deployed, as generate's upgrade note
explains.

Throughput, freshness, lag and custom SLOs have no budget (D5,
opensrm-vcxm), so measure no longer queries anything for them: `evaluate-once`
lists each as `NOT EVALUABLE (no_data_source)`, writes no verdict for it, and
counts them in its closing summary. A request-ratio SLO whose budget series
is missing is logged as a warning (`No error budget value for <service>/<slo>;
not evaluated`) and skipped. That is expected when generate's guard skipped
its budget (generate's README lists when); otherwise generate 4.1.0's rules
are not deployed.

Two other readers of the budget change with it:

- **correlate** reports the service's worst budget with
  `bottomk(1, ...)` rather than `min(...)`, and its `error_budget_exhausted`
  event payload gains `slo`, naming the overspent SLO.
- **drift** (worker mode) analyses only SLOs Prometheus holds a budget series
  for. Judgment SLOs and budget-less classical SLOs no longer log
  `drift_analysis_failed` every cycle; if the lookup itself fails, it logs
  `drift_budget_discovery_failed` once per service and skips that service for
  the cycle.

The `latency_seconds` query kind, its duration-unit table and
`SLODefinition.unit` in `measure/adapters/prometheus.py` are removed.

## 4.3: `evaluate-once` reads reversal rate from the A1 series

`nthlayer-measure evaluate-once` computes a `reversal_rate` judgment SLO from
`nthlayer_reversals_total` over `nthlayer_decisions_total`, the names OpenSRM
Addendum A1 §4.5 gives these counters. It read `gen_ai_overrides_total` over
`gen_ai_decisions_total` before, and there is no fallback to those names.

**Export the `nthlayer_*` counters before upgrading**, each with a `service`
label equal to the manifest's `metadata.name`. If only the `gen_ai_*`
counters exist, a `reversal_rate` SLO returns no value and gets no verdict.
`evaluate-once` now lists every queried SLO that returned nothing as
`NO DATA`, judgment SLOs included, and counts them in its closing summary;
before, only classical SLOs with a missing error budget were listed.

`NO DATA` on a `reversal_rate` SLO has a second, harmless cause: a service
that has counted decisions but no reversal yet. Prometheus has no
`nthlayer_reversals_total` series for it until the first reversal, so the
rate is empty, not zero. That was already so under the old names; it is
only visible now.

Not changed: the worker (`nthlayer-workers serve`) evaluates the query
written in the manifest, so it reads whatever series that query names. The
`high_confidence_failure` query still reads the `gen_ai_*` counters; A1
redefines that rate over outcomes, and it moves in a later release.

## 4.3: one quantity is named reversal rate

OpenSRM Addendum A1's reversals over all decisions
(`nthlayer_reversals_total / nthlayer_decisions_total`) is the only figure
NthLayer calls a reversal rate. Every other figure of that name in
nthlayer-workers is renamed or removed in this release, with no alias: the
old names stop working.

| Where | Old name | Now |
|---|---|---|
| respond `/metrics` | gauge `nthlayer_verdict_reversal_rate` | removed; it was `1 - nthlayer_verdict_accuracy` |
| `nthlayer-measure calibrate --agent`, JSON | `reversal_rate` | `evaluation_override_rate` |
| `nthlayer-measure calibrate --agent`, JSON | `reversal_rate_target`, `reversal_rate_compliant` | removed |
| event `nthlayer.calibration.report` | attribute `reversal_rate` | `evaluation_override_rate` |
| event `nthlayer.calibration.report` | attribute `reversal_rate_compliant` | removed |
| `nthlayer-measure status`, JSON | `reversal_rate` | `evaluation_override_rate` |
| measure config (`measure.yaml`) | `detection.max_reversal_rate` | `detection.max_evaluation_override_rate` |
| measure config (`measure.yaml`) | `agents[].manifest` | removed |
| degradation alert | `metric_name` `reversal_rate` | `evaluation_override_rate` |

Python names, for code that imports nthlayer-workers:

| Old | Now |
|---|---|
| `TrendWindow.reversal_rate` | `TrendWindow.evaluation_override_rate` |
| `SLOThresholds.max_reversal_rate`, `DetectionConfig.max_reversal_rate` | `max_evaluation_override_rate` on both |
| `JudgmentSLOReport.reversal_rate` | `JudgmentSLOReport.evaluation_override_rate` |
| `JudgmentSLOReport.reversal_rate_target`, `.reversal_rate_compliant` | removed |
| `JudgmentSLOChecker(store, slo=...)` | `JudgmentSLOChecker(store, quality_threshold=...)` |
| `emit_calibration_report_event(reversal_rate=..., compliant=...)` | `evaluation_override_rate=...`; `compliant` removed |
| module `nthlayer_workers.measure.manifest` (`JudgmentSLO`, `load_manifest`) | removed |
| `AgentConfig.manifest` | removed |
| `VerdictChainEvidence.downstream_reversal_rate` | `downstream_overridden_ratio` |

What to do:

- **Dashboards and alerts** that read `nthlayer_verdict_reversal_rate` show
  no data after the upgrade, without an error. Use
  `1 - nthlayer_verdict_accuracy` for the same number, or the A1 counters
  for the reversal rate.
- **`measure.yaml`**: rename `detection.max_reversal_rate`, and delete
  `agents[].manifest`. A config that still has either key is refused at
  load, with an error that names the key.
- **Anything that reads the calibrate or status JSON, or the
  `nthlayer.calibration.report` event**: read `evaluation_override_rate`.

`evaluation_override_rate` is overridden evaluations over all evaluations
in the window, counted from the measure score store. It is not
`override_rate` in `calibrate --verdict`, which is overridden verdicts over
resolved verdicts.

`calibrate --agent` no longer reads the agent's manifest and no longer says
whether a reversal-rate SLO is met: it compared the manifest's ceiling for
one quantity against a different one. The reversal-rate SLO is evaluated
from the A1 counters, by the measure module and by `evaluate-once`.

Not changed: the `reversal_rate` judgment SLO type, the manifest keys that
declare it, and the SLO's name in verdicts and alerts.

## 4.3.1: observe reads an SLO only when its query is a good ratio

`observe.collect` and `nthlayer-observe collect` computed an error rate as
`1 - value` for every SLO with a query. That is right for an availability
ratio and wrong for anything else: a latency SLO whose query is a p95 of
0.2 s was reported as an SLI of 20% with its budget `EXHAUSTED`, and a
custom SLO answering 97 (a percentage) as 9700% and `HEALTHY`.

Now only two shapes are evaluated, and only for an SLO that declares a
type: one with both `good_query` and `total_query`, and an availability
SLO (judgment SLOs included) with a single query. Every other SLO gets a `slo_status` reading with status
`NO_DATA`, no SLI and no budget figures, and `error` set to
`not_evaluable: <reason>`. Its query is not sent.

What changes for you:

- A deploy gate that was `BLOCKED` by a latency SLO's false `EXHAUSTED`
  reading clears on the first collection after the upgrade.
- Latency, error-rate, throughput and custom SLOs no longer count toward
  the service's aggregate budget or the portfolio's status counts. They
  were counted with wrong numbers before.
- An SLO that reaches the worker with no `slo_type` is no longer assumed
  to be availability: it gets the same `NO_DATA` reading, and one
  `slo_without_type_not_evaluated` warning.

## Breaking changes

### `gen_ai.decision.evaluated` → per-dimension grader events + a summary

One event per evaluation becomes one `gen_ai.evaluation.result` per scored
dimension, plus one `nthlayer.evaluation.completed` per evaluation.

| Old (`gen_ai.decision.evaluated`) | New |
|---|---|
| one event per evaluation | one `gen_ai.evaluation.result` per dimension |
| — | `gen_ai.evaluation.name` = the dimension |
| — | `gen_ai.evaluation.score.value` = its score |
| — | `nthlayer.evaluator.kind` = `implementation` |
| `task_id` | `nthlayer.decision.id` (omitted when empty) |
| `agent_name` | `gen_ai.agent.name` |
| `eval_id` | `nthlayer.evaluation.id` |
| `evaluator_model` | `nthlayer.evaluator.model` |
| `confidence` | `nthlayer.evaluator.confidence` |
| `dimension_count` | `nthlayer.evaluation.dimension_count` (summary event) |
| `cost_usd` | `nthlayer.evaluation.cost_usd` (summary event) |
| `alert_count` | `nthlayer.evaluation.alert_count` (summary event) |
| — | `nthlayer.evaluation.auto_approved` (summary event) |

Per-evaluation facts are on the summary event only, so cost is no longer
repeated, and summed, once per dimension. An **auto-approved** output emits
the summary and no grader events: no evaluator ran, so its configured scores
are nobody's opinion (A1 §3).

### `gen_ai.override.applied` → a human grader event

A human's corrected dimension score is a `gen_ai.evaluation.result` with
`nthlayer.evaluator.kind` = `human` (A1 §4.4).

| Old (`gen_ai.override.applied`) | New (`gen_ai.evaluation.result`) |
|---|---|
| `dimension` | `gen_ai.evaluation.name` |
| `corrected_score` | `gen_ai.evaluation.score.value` |
| `original_score` | `nthlayer.evaluation.original_score` |
| `corrector` | `nthlayer.evaluator.actor` |
| `eval_id` | `nthlayer.evaluation.id` |
| — | `nthlayer.decision.id` (the evaluated task) |

### Renamed, attributes unchanged

| Old | New |
|---|---|
| `gen_ai.calibration.report` | `nthlayer.calibration.report` |
| `gen_ai.agent.state.changed` | `nthlayer.autonomy.changed` |

### `gen_ai.provider.name` on LLM-call events

Workers used to report `unknown` whenever a model string had no
`provider/` prefix, and always on correlate's summary-failure path. It now
derives the provider: an explicit prefix wins, `claude*` is `anthropic`, and
`gpt*`, `o1*` and `o3*` are `openai`. Anything else is still `unknown`. A
correlate failure with no model configured now reports nthlayer-common's
default model rather than `default`.
