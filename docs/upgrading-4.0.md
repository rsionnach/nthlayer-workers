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
