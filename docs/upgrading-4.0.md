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

## 4.4: observe reads error budgets from generate's recording rules

`observe.collect` and `nthlayer-observe collect` no longer evaluate an SLO's
own query. They read `slo:error_budget_remaining:ratio{service, slo}`, the
series nthlayer-generate's recording rules write, as measure has since
4.2. Availability, latency and error-rate SLOs are read this way.

Before, every SLO's own query was read as a good ratio, which is right for
an availability ratio and wrong for anything else: a latency SLO whose
query is a p95 of 0.2 s was reported as an SLI of 20% with its budget
`EXHAUSTED`, and a custom SLO answering 97 (a percentage) as 9700% and
`HEALTHY`. A deploy gate that was `BLOCKED` by such a reading clears on
the first collection after the upgrade.

**Load the recording rules from nthlayer-generate 4.1.0 or later into
Prometheus before upgrading.** There is no fallback to the manifest's query:

- Until the rules are loaded, every classical SLO reads `NO_DATA` with the
  error `no error budget series`, and the deploy gate returns `WARNING`
  ("No budget data for N SLOs").
- There is no backfill. The rules compute each budget over the SLO's window
  from the samples Prometheus has, so for the first window after the rules
  load, the gate acts on a budget that covers only the time since they were
  deployed.

What else changes:

- **Judgment SLOs** (reversal rate and the like) are no longer read by
  observe: they get `NO_DATA` with `not_evaluable: judgment SLOs are
  evaluated by measure`. Measure evaluates them, from the A1 counters.
  Correlate therefore gets no breach event from observe for a judgment
  SLO; it still gets measure's `quality_breach` verdict.
- **A good/total pair no longer makes an SLO readable.** Throughput,
  freshness, custom and unrecognised kinds are `NO_DATA` `not_evaluable`
  whatever queries they carry, because generate writes them no budget.
- **The objective on a reading is the one the budget was computed
  against**, the `objective` label of the series, not the manifest's
  target. They differ while a changed manifest waits for its rules to be
  regenerated; observe then logs `slo_objective_differs_from_manifest` once
  per service and SLO. `total_budget_minutes`, `burned_minutes` and
  `current_sli` follow the series' objective.
- **The numbers change for availability SLOs.** `percent_consumed` was a
  5-minute error ratio scaled to the whole window; it is now the share of
  the budget used over the window. `current_sli` is the good ratio over the
  window, not over the last 5 minutes.
- **An SLO with no `slo_type`** is no longer assumed to be availability. It
  gets `NO_DATA` with `not_evaluable: no slo_type`, and observe logs
  `slo_without_type_not_evaluated` once per service and SLO.
- **A budget with no requests behind it** (the rule's value is not a
  number) reads `NO_DATA` with `error budget is not a finite number`.
- **The deploy gate warns when it has no budget to judge.** When a service
  has readings and none carries a budget, the gate returned `APPROVED`
  with 100% remaining; it now returns `WARNING`, and `budget_remaining_pct`
  is `null` in the `gate` and `check-deploy` output and in the
  `deploy_gate` assessment. A service with no readings at all is still
  approved by default.

## 4.4: retrospectives are written when the incident is over

`learn.retrospective` wrote a retrospective the moment a correlation
snapshot appeared, from the verdicts on the service inside the snapshot's
window. Respond acts after that window closes, so the retrospective never
contained anything respond did.

Now:

- **The chain is found by lineage**: the snapshot's trigger verdicts, plus
  every verdict that descends from the snapshot. That includes respond's
  triage, investigation, communication and remediation verdicts, and
  excludes other incidents on the same service.
- **The retrospective waits for the incident.** It is written once nothing
  has joined the chain for `--retrospective-quiet-minutes` (default 10) and
  no unresolved case in core is anchored on the chain. An incident whose
  case is still open waits for the case to be resolved.
- **It is written at `--retrospective-max-age-hours` (default 24)
  regardless**, so an incident that is never resolved still gets one.
- **`closed_by`** on the retrospective says which: `quiet` or `max_age`. A
  `max_age` retrospective may describe an incident that is still open.
- **`outcome_coverage`** counts a decision as resolved when core holds an
  outcome for it. It read the decision's own `outcome` field before, which
  core does not update when an outcome is resolved.

Two limits to know about:

- If respond does not act on a snapshot within the quiet period (it is
  stopped, or far behind), there is no case yet to hold the incident open,
  and its retrospective is written as `quiet` with the trigger verdicts
  alone.
- After a workers restart, learn reads core's newest 100 snapshots again.
  None gets a second retrospective: a retrospective's id is now
  `retro-<snapshot id>`, and core refuses a duplicate. A snapshot still
  waiting for its retrospective that is older than those 100 is not picked
  up again.

What to expect: a retrospective now arrives at least the quiet period after
the last verdict, and for an incident with a case, not before the case is
resolved or 24 hours pass. Anything that waits for a retrospective right
after a snapshot needs to allow for that, or lower the two flags.

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
