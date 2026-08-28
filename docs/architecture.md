# nthlayer-workers architecture

Source layout and test-suite cross-reference for the Tier 2 worker
modules: observe, measure, correlate, respond, learn. The hard rules
in `CLAUDE.md` are canonical for runtime invariants — this file is the
"what lives where" reference.

## Top-level entry point

```
src/nthlayer_workers/
  __init__.py       # Package marker: "Tier 2 background computation modules"
  cli.py            # nthlayer-workers serve  + gate subcommand
  runner.py         # ModuleRunner orchestrator (see below)
```

### `runner.py` — ModuleRunner

`WorkerModule` Protocol (`name`, `restore_state`, `process_cycle`,
`get_state`), `RegisteredModule` dataclass, `ModuleRunner(core_url,
instance_id, tick_interval=1.0, injectable client)`.

`run()`:

1. `restore_all_state` — calls each module's `restore_state` with
   prior persisted state (or None if first cycle / API failure).
2. Interval loop — drives each module on its registered cadence.
3. Heartbeat only when ≥1 module ran in the tick.
4. SIGTERM / SIGINT shutdown → `persist_all_state` + final heartbeat.

State restored via `result.ok` check on `get_component_state`. State
persisted via `CoreAPIClient.put_component_state` after each cycle.
Cycle failures are swallowed (runner continues).

### `cli.py` — serve / gate

`nthlayer-workers serve` flags: `--core-url`, `--instance-id`,
`--prometheus-url`, plus per-module intervals (collect, drift,
topology, correlate, topology-drift, contract, measure, respond,
outcome, retrospective), `--expiry-threshold-days=7`,
`--min-resolution-age-hours=1`, `--tempo-endpoint`.

`gate --service X [--tier critical] [--commit-sha Y] [--core-url]`:
CLI-only deploy gate, no HTTP API in v1.5. Exit codes:
0 = APPROVED/WARNING, 1 = eval error, 2 = BLOCKED.

Gate flow: resolve tier from manifest → fetch slo_status →
`MemoryAssessmentStore` → `check_deploy()` → submit `deploy_gate`
assessment with `parent_ids=slo_ids`.

Serve registers (and the intervals are tunable per the flags above):

- `ObserveCollectModule` (60s)
- `ObserveDriftModule` (1800s)
- `ObserveTopologyModule` (86400s)
- `CorrelateSessionModule` (10s)
- `CorrelateTopologyModule` (3600s)
- `CorrelateContractModule` (3600s) [P3-D.2]
- `MeasureModule` (60s) [P3-C.1]
- `RespondModule` (30s) [P3-E.1]
- `LearnOutcomeModule` (60s)
- `LearnRetrospectiveModule` (30s) [P3-F.1]

## `observe/` — deterministic runtime infrastructure

SLO assessment, drift, discovery, gate, portfolio, verification.

### Key modules

- `assessment.py` — `Assessment(id, created_at, kind, service,
  producer, data)`. `VALID_ASSESSMENT_TYPES = ASSESSMENT_KINDS` (from
  `nthlayer_common.cloudevents`) `| frozenset({"verification"})`.
  `verification` is observe-internal (CLI-only, never submitted to
  core). P3-D.1 moved `correlation_snapshot` / `topology_drift` /
  `contract_divergence` into common ASSESSMENT_KINDS. P3-B.2 renames:
  `gate → deploy_gate`, `dependency → dependency_graph`.
  `create(kind, service, data, *, producer) -> id
  "asm-{date}-{uuid8}-{seq:05d}"`. `to_dict/from_dict` (created_at
  ↔ ISO string).
- `cli.py` — `nthlayer-observe` CLI (10 commands: collect, drift,
  verify, discover, dependencies, blast-radius, portfolio, scorecard,
  check-deploy, explain). `_add_decision_store_args(parser)` adds
  `--decision-store` + `--no-legacy-store` flags to subcommands.
  `_write_decision_record(args, legacy, *, incident_id=None)`
  dual-writes content-addressed record when `--decision-store` set;
  chain fork logged at error, other errors at warning (fail-open).
  collect + drift both call `_write_decision_record` after storing.
- `sqlite_store.py` — `SQLiteAssessmentStore`. Thread-local
  connections, WAL mode, 5s busy_timeout. Schema:
  `assessments(id, created_at, kind, service, producer, data TEXT)`
  + indexes on `created_at` / `service` / `kind` / `(service, kind)`.
  `_migrate_if_needed` drops table + stale indexes
  (`idx_assessments_timestamp` / `idx_assessments_type` /
  `idx_assessments_svc_type`) if pre-P3-B.1 schema (`assessment_type`
  col present or `kind` col absent). `put` raises ValueError on
  duplicate. `query`: dynamic WHERE + ORDER BY created_at DESC,
  `limit=0` returns all. Can share same db file as
  `SQLiteVerdictStore`.
- `store.py` — `AssessmentStore` ABC (put / get / query /
  get_latest); `MemoryAssessmentStore` (thread-safe, tests);
  `AssessmentFilter(service, kind, producer, from_time, to_time,
  limit=100)`. P3-B.1 rename complete: field is `kind` (not
  `assessment_type`).
- `worker.py` — P3-B.2 splits into three WorkerModule impls
  (`ObserveCollectModule` + `ObserveDriftModule` +
  `ObserveTopologyModule`):
  - `ObserveCollectModule.process_cycle()`: GET /manifests →
    `_extract_service_slos` → group by service →
    `SLOMetricCollector.collect` → `results_to_assessments`
    (`kind="slo_status"`) → POST /assessments (collection_errors
    counted, don't abort) → `build_portfolio_from_results()` → POST
    `portfolio_status` (`service="__portfolio__"`,
    `parent_ids=slo_assessment_ids`).
  - `ObserveDriftModule.process_cycle()`: iterates
    `_drift_targets(manifests)`, `DriftAnalyzer.analyze()` per
    `(service, tier, slo_name, window)`, submits `drift_signal`
    assessments.
  - `ObserveTopologyModule`: `DependencyDiscovery` +
    `PrometheusDepProvider`. `discovery.build_graph(service_names)`
    once → per-service `calculate_blast_radius` → submits
    `dependency_graph` assessments
    `{total_services_affected, critical_services_affected, risk_level,
    direct_downstream_count, recommendation}`. Scale note: single
    graph per cycle fine for v1.5; bulk API needed at 100+ services.
  - All three share `CoreAPIClient` + `prometheus_url`.
    `_fetch_manifests()` shared helper (None on error, [] on empty).
    `_extract_service_slos()` parses manifest dicts from GET
    /manifests (worker path, parallel to `spec_loader.load_specs()`
    which reads YAML on disk for CLI).
- `gate_adapter.py` — `CoreAPIAssessmentStore`: read-only
  AssessmentStore backed by `CoreAPIClient`. `put()` raises
  `NotImplementedError("read-only")`, `get()` raises
  `NotImplementedError("not needed by gate")`, `query()` uses
  `asyncio.run(client.get_assessments())`, returns `[]` on API
  failure. CLI-only (safe for no running event loop). `cli.py gate`
  command uses `MemoryAssessmentStore` directly — `gate_adapter` is a
  utility for other callers.
- `decision_records.py` — Bridge: legacy observe `Assessment` →
  content-addressed decision records.
  `build_decision_record(legacy, *, previous_hash, incident_id?) ->
  records.Assessment` (SHA-256 hash from canonical JSON).
  `build_stream(legacy)`: `"sli:{service}:{slo_name}"` for
  `slo_status` / `drift_signal`, else `"{kind}:{service}"`.
  `map_severity(type, data) -> records.Severity` via lookup dicts
  (`_SLO_STATUS_SEVERITY`, `_DRIFT_SEVERITY`, `_GATE_SEVERITY`).
  `generate_summaries(legacy) -> Summaries` (technical / plain /
  executive, truncated 280 / 280 / 140 chars) for 6 kinds. `_TYPE_MAP`:
  slo_status → THRESHOLD_BREACH, drift_signal → DRIFT,
  deploy_gate / dependency_graph / portfolio_status / verification →
  CHANGE_EVENT.
- `explanation.py` — `ExplanationEngine`: builds human-readable
  `BudgetExplanation` objects from `slo_status` assessments enriched
  with drift context (opensrm-pku).
  `explain_service(service, store, slo_filter?) ->
  list[BudgetExplanation]` — deduplicates keeping latest per SLO name
  (desc order), calls `_latest_drift_by_slo` then passes drift per
  SLO to `_explain_slo`.
  `_latest_drift_by_slo(service, store) -> {slo_name: drift_data}` —
  queries `kind="drift_signal"` bounded to service, `limit=0`,
  deduped to latest per SLO.
  `_explain_slo(service, assessment, drift: dict | None = None) ->
  BudgetExplanation`. Cold-start: `drift=None` silently skipped.
  `_STATUS_SEVERITY`: EXHAUSTED / CRITICAL → "critical",
  WARNING / ERROR → "warning", HEALTHY / NO_DATA / UNKNOWN → "info".
  `_DRIFT_PATTERN_CAUSE` lookup dict maps `DriftPattern` enum values
  → cause-string templates (6 patterns: gradual_decline /
  gradual_improvement / step_change_down / step_change_up / seasonal /
  volatile; stable omitted → no cause).
  `slope_pct = slope_per_week * 100` filled into
  `{slope_pct:.2f}%/week` templates.
  `days_until_exhaustion` appended as separate cause when
  `isinstance(int)` and `>= 0` ("Projected exhaustion in N days at
  current trend"). Headline: `"{slo_name}: {pct:.0f}% consumed —
  {desc} ({status})"`. Body: window + budget totals. SLI and
  objective in 0–100 range (collector.py multiplies by 100). Causes:
  >80% consumption or SLI < objective (gap in pp) + drift causes.
  Actions: EXHAUSTED → gate block warning, CRITICAL / EXHAUSTED →
  investigate + freeze, WARNING → monitor. Deterministic, no LLM.
  Imports `BudgetExplanation` from `nthlayer_common.explanation`.
- `portfolio/aggregator.py` — `build_portfolio(store)` for CLI (reads
  `slo_status` assessments from `AssessmentStore`).
  `build_portfolio_from_results(results_by_service)` for
  `ObserveCollectModule` in-memory pass (no store round-trip). Both
  delegate to `_summarise_services()` — single shared impl.
  `PortfolioSummary(services, total_services, healthy_count,
  warning_count, critical_count, exhausted_count)`. UNKNOWN /
  NO_DATA / ERROR silently excluded from counts (not tracked).
  `_worst_status` uses `_STATUS_SEVERITY` dict: EXHAUSTED=4,
  CRITICAL=3, WARNING=2, ERROR=1, NO_DATA=0, HEALTHY=-1, UNKNOWN=-2.
- `slo/collector.py` — `SLOMetricCollector(prometheus_url)`:
  stateless, async `collect(list[ServiceSLO]) -> list[SLOResult]`.
  `PrometheusProvider` closed in finally. Auth from
  `PROMETHEUS_USERNAME` / `NTHLAYER_METRICS_USER` +
  `PROMETHEUS_PASSWORD` / `NTHLAYER_METRICS_PASSWORD`.
  `sli_value=0.0` treated as total outage (EXHAUSTED) not NO_DATA
  (opensrm-e1gk tracks provider fix).
  `_build_slo_query`: `indicator_query` first, then
  `good_query` / `total_query` fallback, substitutes `$service` /
  `${service}`.
  `_determine_status`: lookup dict `STATUS_THRESHOLDS`
  `{100→EXHAUSTED, 80→CRITICAL, 50→WARNING, default HEALTHY}`.
  `_parse_window_minutes`: lookup dict `{d, h, w, m}`, default
  30d + structlog warning.
  `calculate_aggregate_budget(results) -> BudgetSummary`.
  `results_to_assessments(results, service) -> list[Assessment]`
  (`kind="slo_status"`).

## `measure/` — LLM-powered AI decision quality evaluation

`worker.py` — `MeasureModule` WorkerModule impl [P3-C.1].
`process_cycle()`:

1. Fetch manifests from core.
2. Per judgment SLO (skips SLOs without `judgment_type` field): query
   Prometheus → submit `judgment_slo_evaluation` assessment
   (`id="jse-{service}-{slo_name}-{uuid8}"`) → detect
   HEALTHY → BREACH transitions → emit `quality_breach` verdict
   (`id="vrd-breach-{service}-{slo_name}-{uuid8}"`, includes
   `severity` field `"low"|"high"|"critical"`).
3. Per new breach: call `_evaluate_governance()` once → emit
   `autonomy_change` verdict (`id="vrd-auto-{service}-{uuid8}"`) if
   reduced.

State shape: `{slo_status: {slo_key: "breach"|"healthy"},
breach_decisions: {slo_key: {decided, decided_at, autonomy_action}},
autonomy_levels: {service: level}, breach_severities: {slo_key:
"low"|"high"|"critical"}}` (no cursor).

Five autonomy levels [P3-C.2]: fully_autonomous → autonomous →
limited_autonomous → advisor → observer. Severity-based reduction
rules (deterministic, no LLM in v1.5): low → drop 1 level,
high → drop 2 levels, critical → drop to observer. Multi-breach: any
critical → observer; else → advisor.

Severity per judgment SLO type via `classify_severity()` (module-level
in `measure/worker.py`):

- Budget-consumption types (reversal_rate / high_confidence_failure /
  escalation / outcomes / audit_sampling): <200% = low / 200-500% =
  high / >500% = critical.
- Stability: any breach = high.
- Segments: variance-based (`_classify_variance`).
- Calibration: delta-based (`_classify_calibration`).
- Unknown type defaults to "high".

`_BUDGET_CONSUMPTION_TYPES = frozenset` of 5 types. P3-C.3 implemented
as `ModelEvaluator` in `pipeline/evaluator.py` (Instructor-backed
quality scoring of agent outputs — separate from SLO governance).
`_compute_governance()` remains deterministic (no LLM in v1.5).

Cold start treats all SLOs as "unknown" (one false positive per
pre-existing breach acceptable). Governance called once per breach via
`_compute_governance()` → returns `(action, steps)` tuple.
`_breach_decisions` and `_breach_severities` cleared on HEALTHY
recovery. `_autonomy_levels` is a **one-way ratchet** — NOT cleared on
recovery. Transient Prometheus failure carries forward previous status
for missing SLO keys (prevents false transitions).

SLI >= target = healthy (inverted: reversal_rate SLI is
`1 - reversal_rate`). All three output types already in
ASSESSMENT_KINDS / `_VERDICT_TYPES` (no taxonomy changes).

**SLO target convention** (opensrm-5fff.1): `_evaluate_slo` scales
Prometheus SLI (always 0.0-1.0 ratio) to percentage via
`current_pct = current_value * 100` before comparing to target (which
is 0-100 percentage canonical). `_classify_budget_consumption` uses
`error_budget_pct = 100.0 - target` (e.g. target=98.5 → budget=1.5pp;
current=92.0 → consumption=433% → "high"). Guard
`if error_budget_pct <= 0: return "critical"` (target=100 means zero
budget).

`cli.py` — `nthlayer-measure` CLI. Live subcommands: `evaluate` /
`evaluate-once` / `status` / `calibrate` / `overrides {list, create}` /
`tiering {show, restore}`. The legacy `serve` / `api-serve` /
`governance {show, restore}` subcommands (and the `_build_pipeline`
helper they shared) were retired under opensrm-t5yr —
`nthlayer-measure serve` superseded by `nthlayer-workers serve`
(P3-C.1), `api-serve` superseded by core's HTTP API, governance
superseded by the deterministic severity-based path in
`measure/worker.py` (P3-C.2). Bare `nthlayer-measure` with no
subcommand now prints help and exits 0.

`config.py` — `MeasureConfig` dataclass tree + `load_config(path) ->
MeasureConfig`. `VALID_TOP_KEYS` frozenset of 8 recognised top-level
YAML keys (evaluator / store / detection / dimensions / agents /
verdict / trigger / tiering). `load_config` raises ValueError on
unknown top-level key (lists offenders + valid set, sort uses
`key=str` so mixed-type keys don't crash) and on non-mapping root.
`governance:` (retired under opensrm-t5yr) is treated as any other
unknown key — no deprecation grace [opensrm-m655].

`adapters/prometheus.py` — `_query_for(service, slo) -> (query,
query_kind)`, the single query builder. The kind rides on
`SLODefinition.query_kind` (no default — a wrong one is silent) and is
what `evaluate_slos` dispatches its breach rule on: `judgment_rate`
inverts a 0-1 rate to an SLI, `ratio` scales a good-ratio SLI without
inverting, `judgment_duration` and `latency_seconds` compare durations,
`error_budget` tests a signed budget against zero. Dispatching on the
SLO's name or `slo_type` instead is what broke: the name is author-chosen
in v2, and half the judgment taxonomy has no rate builder, so neither
answers what units the value is in [opensrm-fxln]. Module-level `_JUDGMENT_SLO_QUERIES` lookup
dict of lambdas keyed by **`spec.judgment_type`**, not by SLO name: in
v2 `metadata.name` is author-chosen and independent of the type
(`reversal_rate` / `high_confidence_failure` / `calibration` /
`feedback_latency`). Only 4 of the 8 `JUDGMENT_SLO_TYPES` have a
builder; the other 4 fall through to the `slo:{name}:ratio` recording-
rule convention. They must NOT yield `""` — Prometheus 400s on an empty
query and `query_prometheus` reads that as no-data, silently skipping
the SLO (opensrm-fxln; the `_judgment_slo_query` wrapper that returned
`""` was deleted there). Hysteresis reads each window's `raw_breach`
back out of the verdict blob built by `evaluation_custom_metadata` —
one definition shared by the CLI writer and
`count_consecutive_breaches`, which previously re-derived breach as
`current_value > target`, a 0-1 rate against a 0-100 target, so it never
counted a judgment window and the threshold was unreachable. Verdicts
written before opensrm-fxln carry no `raw_breach` and stop the count:
one restarted hysteresis window per SLO at upgrade. The history window
is sized `(hysteresis_threshold + 1) * len(slos) * 2` rather than a flat
20 — one cycle writes one verdict per SLO, so a constant limit held less
than a full cycle once a run covered 20 SLOs and capped `consecutive` at
1 again. It is deliberately unscoped by `VerdictFilter.subject_service`:
measure writes the service into `subject.ref` and leaves
`subject.service` None, so that filter matches nothing, and populating
it now would silently shorten history for every SLO already running.
Duration targets convert via `_target_seconds` using the manifest's
declared `unit`, which the parser preserves; a missing or unknown unit
logs a warning and assumes `ms` rather than dropping the SLO.
`calibration` +
`feedback_latency` are
window-agnostic (gauge metrics) so their lambdas use a `_window`
underscore param. The module logs through `logging.getLogger`, NOT
structlog — %-style args only; kwargs would TypeError (load-bearing
fact per opensrm-y7dd R5 Pass 3).

`governance/` — `GovernanceEngine(Protocol)` only. Legacy
LLM-driven `ErrorBudgetGovernance` retired under opensrm-t5yr; the
live autonomy decisions live in `worker.py` (deterministic
severity-based, P3-C.2).

`notifications.py` — `build_breach_blocks(verdict) -> (blocks,
fallback_text)`: Slack Block Kit for SLO breach notifications.
`target` and `current` stored in 0-100 percentage convention
(opensrm-5fff.1) — displayed directly as `f"{value:.1f}%"` without
`*100` conversion.

`pipeline/evaluator.py` — P3-C.3 Instructor-backed LLM evaluator.
`Evaluator` Protocol: async `evaluate(output, dimensions, model?) ->
QualityScore`. `ModelEvaluator(model, max_tokens=4096, timeout=30.0)`:
`build_prompt()` from `prompts/evaluator.yaml` via `load_prompt` /
`render_user_prompt`. `evaluate()` calls `structured_call_with_usage`
via `asyncio.to_thread` + `asyncio.wait_for(timeout+5s)`,
`max_retries=3`. `DimensionScore(score [0-1], reasoning="")` +
`EvaluationResult(dimensions, confidence=0.0)` Pydantic models for
Instructor. `_compute_cost`: `_MODEL_PRICING` lookup dict
(sonnet-4: 3.0/15.0, haiku-4: 0.80/4.0, opus-4: 15.0/75.0 per M
tokens), None for unknown models. `_to_quality_score`: empty reasoning
strings excluded from `QualityScore.reasoning` dict. Prompt path:
`prompts/evaluator.yaml` (4 levels up from file).

## `correlate/` — session window-based event correlation

`types.py` — `EventType(ALERT / METRIC_BREACH / CHANGE /
QUALITY_SCORE / VERDICT / CUSTOM)`. `SitRepEvent(id, timestamp,
source, type, service, environment, severity 0.0-1.0, payload,
dependencies, dependents, ttl=86400)`. `TemporalGroup`,
`ChangeCandidate`, `TopologyCorrelation`, `CorrelationGroup`,
`AgentState`.

`session.py`:

- `CorrelationDomain(service, environment)` — frozen dataclass,
  hashable dict key.
- `SessionWindow(domain, events, opened_at, last_event_at,
  has_trigger)` — closes on gap (default 60s), max_duration (default
  900s), or trigger (`quality_breach`). `close_reason()` priority:
  trigger > max_duration > gap. `add_event()` updates
  `last_event_at` from event timestamp. **Stale-backlog protection:**
  `opened_at` capped at `min(event_ts, now)`.
- `SessionWindowManager` — `ingest()`, `close_ready(now)`,
  `restore_window()`, `to_state()` for crash recovery (events
  re-fetched, only metadata persisted).

`worker.py` — P3-D.2 splits into three WorkerModule impls:

- `CorrelateSessionModule` (renamed from `CorrelateModule`, no
  functional change): `process_cycle()` polls GET /verdicts
  (`quality_breach`, `autonomy_change`) + GET /assessments
  (`slo_status`, `drift_signal`) → `verdict_to_event` /
  `assessment_to_event` → `SessionWindowManager.ingest` →
  `close_ready` → `_emit_snapshot`. Cursor tracks most-recent event
  timestamp. v1.5 limitation: assessments API has no `created_after`
  filter — client-side filter, misses beyond page boundary (tracked
  constraint). `_emit_snapshot`: builds `correlation_snapshot`
  assessment `{domain, window metadata, event_count, event_types,
  peak_severity, affected_services, environment_source="default",
  nl_summary=await generate_summary(snapshot_data, window.events)`
  (non-blocking, 5s timeout — P3-D.3 implemented). Id format
  `"csn-{service}-{environment}-{uuid4hex8}"`. Orphaned empty
  windows skipped.
  - `verdict_to_event`: `quality_breach` → QUALITY_SCORE + 0.9;
    others → VERDICT + 0.5.
  - `assessment_to_event`: EXHAUSTED / CRITICAL → METRIC_BREACH;
    others → ALERT. Severity: EXHAUSTED=1.0, CRITICAL=0.8,
    WARNING=0.5, HEALTHY=0.1, default=0.3.
  - Environment hardcoded `"production"` (v2 adds env to assessment
    model).
- `CorrelateTopologyModule` (P3-D.2, 1h): lazy-imports
  `detect_topology_divergence()` from
  `nthlayer_workers.correlate.traces.topology`; also calls
  `_check_guarantee_mismatches()` for edge-level SLO checks. Emits one
  `topology_drift` assessment per cycle (not per-service):
  `id="tdr-topology-{uuid8}"`, `service="__topology__"`.
  `_logged_no_backend` flag prevents repeat logging when Tempo not
  configured. Three drift categories: `declared_not_observed` /
  `observed_not_declared` / `guarantee_mismatches`.
- `CorrelateContractModule` (P3-D.2, 1h): queries SLO indicator
  expressions via `PrometheusProvider` (opened per cycle, closed in
  finally). Availability promise must be ratio 0.0–1.0 (>1.0 skipped
  with warning). `observed=None` → no violation (no-data not an
  outage). `observed=0.0` IS a violation (total outage,
  opensrm-e1gk). Latency query result in seconds × 1000 for ms
  comparison. Emits `contract_divergence` assessment
  `id="cdv-{service}-{uuid8}"` only on divergence. Skips services
  with no contracts.

Module-level helpers in `worker.py`:
`_check_guarantee_mismatches(manifests, evidence) -> list[dict{source,
target, metric, promised, observed, tempo_window="1h"}]`.
`_parse_latency_ms(str) -> float | None` (`"200ms"` → 200.0,
`"1s"` → 1000.0).

`summary.py` — P3-D.3 NL summary generation for correlation snapshots.
`SnapshotSummary` Pydantic model: `summary: str max_length=500`,
`notable_omissions: list[str] default []`,
`confidence: float = Field(ge=0.0, le=1.0, default=0.0)`
(opensrm-w7q.3 acceptance criterion).

`generate_summary(snapshot_data, events, model=None) -> dict | None`:
async, 5s `SUMMARY_TIMEOUT` via `asyncio.to_thread(structured_call())`.
Returns `{"summary": ..., "notable_omissions": [...], "confidence":
float}` or None on any failure (non-blocking). `SYSTEM_PROMPT` defines
confidence as groundedness in supplied sample events: 1.0 = every
claim cites a sample event, 0.0 = generic. Instructs model to lower
confidence when `notable_omissions` is non-empty.

`_select_sample_events(events, max_samples=10)`: first event + most
severe + most recent per service, deduplicated by ID, capped at
`max_samples`. `_record_failure`: structlog warning +
`errors_total.labels(component="correlate",
error_type="summary_{reason}").inc()` + `emit_llm_event` OTel — all
lazy-imported, non-blocking. `_classify_failure`:
`TimeoutError → "timeout"`, `"validation"` / `"pydantic"` in
classname → `"validation_error"`, else → `"llm_unavailable"`.
SYSTEM_PROMPT: 2-4 sentence SRE-facing observations, no root-cause
speculation. Imports `structured_call` from
`nthlayer_common.llm_structured`.

`cli.py` — `nthlayer-correlate` CLI. Subcommands: `serve` (continuous
snapshot generation via `_serve_loop` — opens SQLiteAssessmentStore
from `config.verdict_store_path`, opensrm-saun.1.2.1 removes
`nthlayer_learn.store.VerdictStore`, persist via
`SQLiteAssessmentStore` sharing verdict-store db file), `replay`
(replay scenario fixture via `replay_command` — calls
`model.interpret()` which returns `Assessment` instances), `status`
(show event store stats), `correlate` (live triggered — writes
`correlation_snapshot` ASSESSMENT to `SQLiteAssessmentStore` sharing
verdict store db file; `--reasoning` / `--no-reasoning` mutually
exclusive group (default reasoning=True); `--model` flag for
reasoning model override; `--trace-backend choices=["tempo"]` +
`--tempo-endpoint` + `--trace-detail choices=["summary","full"]`;
when `--trace-backend=tempo`, instantiates
`TempoTraceBackend(endpoint, use_service_graphs=True)`;
`correlate_command()` accepts trace_backend Protocol +
`trace_baseline_window="1h"`; `_gather()` queries
`get_trace_evidence(services, start, end, baseline_window)` then
closes backend via `aclose()`; topology divergence computed
post-gather via `detect_topology_divergence` from `traces.topology`;
`evidence_sources` dict includes `trace_backend` backend name +
`query_time_ms`; `--respond-args` forwarding guards against
injection — only allows keys `{"specs-dir", "config", "notify"}`;
decision_store_path write skipped (`write_decision_assessment` helper
pending, opensrm-saun.1.2.1); Slack notification skipped on assessment
path (opensrm-saun.1.2.1)).

`_proximity_confidence`: linear decay 1.0→0.0 over 30-minute window
(uses `nthlayer_common.parsing.clamp`). `_parse_duration`: lookup
dict `{ms, s, m, h, d, w}` multipliers (no regex).
`scenario_event_to_sitrep`: `parse_relative_time` parses `"T+Nm"`
format.

`reasoning.py` — `reason_about_correlations`: used by `correlate`
subcommand (live triggered path) — produces verdict. Distinct from
`snapshot/model.py` `ModelInterface` which is used by serve / replay
(cold path, produces assessments).

`snapshot/model.py` — `ModelInterface`. Cold-path ZFC judgment
boundary (opensrm-saun.1.2.1). Used by serve + replay subcommands.
`interpret(prompt, groups, assessment_store=None) ->
list[Assessment]` (Assessment dataclass from
`observe/assessment.py`, not dicts). `assessment_store` param
renamed from `verdict_store` (breaking for legacy callers).
`_build_child_assessment(parsed) -> Assessment(id="csn-{service}-{uuid8}",
kind="correlation_snapshot", producer="nthlayer-correlate",
data={group_id, summary, action, confidence, reasoning, tags})`.
`_build_parent_assessment(children, *, group_count, degraded=False) ->`
roll-up `Assessment(service="__snapshot__", data.parent_ids=[child.id,
...])` — lineage anchor for downstream consumers. Degraded mode:
`confidence=0.0`, `tags=["degraded"]`, `reasoning="template-based,
model unavailable"`. Empty groups → `[]` (no vacuous parent emitted).
Valid actions: `{"flag", "escalate", "defer"}`. `escalate` in any
child bubbles to parent `action="escalate"`. Cold-path aligns with
hot-path `CorrelateSessionModule` which already used
`submit_assessment`.

## `respond/` — LLM-powered incident response

`cli.py` — `nthlayer-respond` CLI. Subcommands: `serve`
(`ApprovalServer`), `status`, `replay` (`--scenario`, `--config`,
`--no-model`), `approve <incident_id> [--approved-by]`, `reject
<incident_id> --reason [--rejected-by]`, `resume <incident_id>`,
`respond --trigger-verdict --specs-dir --verdict-store [--notify]
[--model]`. SRE experience subcommands: `oncall [--specs-dir]`,
`brief <incident_id> [--verdict-store]`, `shift-report --from <ISO>
--to <ISO> [--verdict-store]`, `suppress <service> <metric> --window
<hh:mm-hh:mm> --reason <str> --baseline <float> [--multiplier
<float>]`, `post-incident <incident_id> [--verdict-store]`,
`delegate <incident_id> [--safe-actions-only] [--max-duration
<Nh|Nm>] [--delegated-by]`. Mock support: `_make_mock_call_model`
(single response) + `_make_sequenced_mock` (successive responses per
call index) for `--no-model` replay. opensrm-saun.1.2.1:
`_build_incident_context` emits mock `csn-*` assessment id (not
verdict id) for `trigger_verdict_ids` in `--no-model` mode.
`"sitrep"` accepted as alias for `"nthlayer-correlate"` in
`trigger_source`.

`config.py` — `RespondConfig`: existing fields + P3-E.1 worker-mode
additions (`cycle_interval_seconds=30.0`,
`fallback_threshold_seconds=60.0`,
`terminal_retention_seconds=86400.0`, `step_timeout_seconds=90.0`).
All four validated `>= 0` in `__post_init__` (negative silently
inverts cutoff semantics). YAML key path `workers.respond.*`.

`coordinator.py` — P3-E.1 changes:

- `AWAITING_APPROVAL` early-return guard in `_run_pipeline`
  (idempotent under worker polling — resumption is P3-E.3).
- Per-step `asyncio.wait_for(step_timeout_seconds)` →
  `IncidentState.FAILED` on timeout.
- `context.error` format convention `"<reason>: <details>"` (e.g.
  `"step_timeout: triage exceeded 90s"`,
  `"unrecoverable: <exc>"`).
- Escalation gate reads `metadata["escalation_pending"]` (set at
  write-time, no verdict re-fetch).
- PIPELINE: `[TRIAGE] → [INVESTIGATION, COMMUNICATION] →
  [REMEDIATION] → [COMMUNICATION]` (4 steps, step 1 parallel).
- Bead 1: `_build_approval_custom(action, target, approved_by) ->
  {"proposed_action": action, "target": target, "approved_by":
  approved_by or "human"}` — fixed 3-key shape on both `approve()`
  success and failure paths so downstream consumers pattern-match
  without defensive `in` checks.
- `_run_parallel_step` uses `zip(roles, results, strict=True)` on the
  `asyncio.gather` fan-in (opensrm-po23) — gather contractually
  preserves task order and count, so `strict=True` surfaces any
  future refactor that breaks the invariant loudly instead of
  silently truncating.

`context_store.py` — `SQLiteContextStore` remains for legacy CLI.
`incident_context_to_dict` / `incident_context_from_dict` moved to
module-level public exports (used by `worker.py` for
`component_state` roundtrip). `from_dict` validates required fields
(`id`, `state`, `created_at`, `updated_at`, `trigger_source`) —
corrupt blobs raise ValueError so `restore_state` can skip
gracefully. `verdict_chain` preserved across roundtrip (load-bearing
for lineage continuity post-restart).

`server.py` — `ApprovalServer` (Prometheus metrics on `:8090`).

`verdict_submission.py` — P3-E.1:
`submit_verdict_to_core(client, verdict, *, deployment_id) -> bool`.
Calls `to_dict(verdict)` before `wrap_verdict` — serialises Verdict
dataclass to wire-canonical dict (id / type / created_at field
names). Wraps in CloudEvents envelope via
`wrap_verdict(component="respond")`. Submits via
`CoreAPIClient.submit_verdict()`. Never raises — failure logged +
`errors_total(component="respond",
error_type="verdict_submit").inc()`. Returns False on failure so
caller skips appending to `verdict_chain` (keeps in-memory chain
consistent with core lineage, avoids dangling `parent_ids`).

`worker.py` — P3-E.1 implemented. `RespondModule(WorkerModule)`:
`process_cycle()` = `_ingest_triggers` → `_drive_active_incidents`.
`Cursors` dataclass (`snapshot_after`, `breach_after` ISO 8601).
`_NoopContextStore`: `save()=noop`, `load()` / `list_active()` /
`list_all()` raise `NotImplementedError` (worker owns state via
`_incidents` — loud failure on misuse beats silent wrong-answer).
`_is_terminal_and_aged()`: prunes RESOLVED / ESCALATED / FAILED
incidents older than `terminal_retention_seconds`; handles naive
datetime (`replace tzinfo=UTC`), malformed timestamp → not-aged
fail-open (debug log).

Trigger ingestion:

- Primary = `correlation_snapshot` assessments (cursor
  `snapshot_after`).
- Fallback = `quality_breach` verdicts older than
  `fallback_threshold_seconds` with no associated snapshot (cursor
  `breach_after`).

`_ingest_orphan_breaches`: guard `breach_after >= cutoff_iso` →
return early (prevents clock-skew / stalled-worker scenario where
`created_after > created_before` at core API level). Breach dedup via
`breach_ids_with_snapshots(snapshot_cache)` set-membership lookup —
NOT per-breach HTTP fetch — avoids fan-out (v1.5 limitation: snapshots
beyond page boundary not matched, tracked for v2).
`_incidents_triggered_from()` union of `trigger_verdict_ids` across
active incidents (cursor-hiccup dedup). Incident opening delegates to
`worker_helpers.open_from_snapshot` / `open_from_breach`.

State blob: `component_state("respond") = {cursors: {snapshot_after,
breach_after}, incidents: {id → IncidentContext}}`. `get_state()`
prunes terminal-and-aged incidents. `parent_ids` on first incident
verdict = `trigger_verdict_ids` (cross-module lineage bridge).
Escalation flag `metadata["escalation_pending"]` set at write-time
(no per-step API round-trips).

Single-instance contract: v1.5 must run exactly one workers process
per core (no lease; multi-instance HA is v2). Pre-designed split:
`RespondTriggerModule` (10s) + `RespondPipelineModule` (30s).
`SQLiteVerdictStore` + `SQLiteContextStore` removed from worker path
(kept for legacy CLI, deleted Phase 4).

`worker_helpers.py` — P3-E.1:

- `open_from_snapshot(snap, config, *, tiers) -> IncidentContext`
  (`trigger_source="nthlayer-correlate"`, rich metadata:
  `blast_radius` / `correlation_summary` / `peak_severity` /
  `event_count` / `affected_services` / `escalation_threshold`).
- `open_from_breach(breach, config, *, tiers) -> IncidentContext`
  (`trigger_source="nthlayer-measure-fallback"`,
  `metadata.fallback_reason="no_correlation_snapshot"`).
- `_make_incident_id(service) -> "INC-{SERVICE}-YYYYMMDD-HHMMSS-{uuid8}"`
  (uuid suffix disambiguates same-second opens).
- `filter_after(records, cursor)`: records with `created_at > cursor`.
- `breach_ids_with_snapshots(snapshot_page) -> set` of breach IDs
  with an associated snapshot (used for fallback dedup without
  per-breach HTTP fetch).

`agents/response_models.py` — Pydantic BaseModel classes for
Instructor-backed structured calls (P3-E.2). One model per agent.
Field names match canonical `parse_response` dict keys. Aliases for
legacy field names live in `parse_response`, not here:

- `TriageResponse(severity int 0-4, blast_radius list[str],
  affected_slos list[str], assigned_team str|None, reasoning,
  confidence)`.
- `HypothesisModel(description, confidence, evidence list[str],
  change_candidate str|None)`.
- `InvestigationResponse(hypotheses list[HypothesisModel],
  root_cause str|None, root_cause_confidence, reasoning,
  confidence)`.
- `CommunicationUpdateModel(channel default "status_page",
  update_type default "initial", content)`.
- `CommunicationResponse(updates list[CommunicationUpdateModel],
  reasoning, confidence)`.
- `AutonomyReductionRequest(recommended bool, target_agent, reason)`.
- `RemediationResponse(proposed_action str|None, target str|None,
  risk_assessment, requires_human_approval bool default True,
  autonomy_reduction AutonomyReductionRequest, reasoning,
  confidence)`.

`agents/base.py` — `AgentBase` ABC. Transport here, judgment in
subclasses. `__init__` accepts `client` (CoreAPIClient, worker mode)
or `verdict_store` (legacy CLI mode) — exactly one must be set
(raises ValueError if neither or both). `deployment_id` kwarg for
CloudEvents envelope. Class attr `response_model: type | None = None`
— when set by subclass, `execute` routes through
`_call_model_structured` (Instructor + validation retry + OTel cost
event) instead of raw text path.

`_call_model`: `asyncio.wait_for` + `asyncio.to_thread(llm_call)`.
`_call_model_structured(system_prompt, user_prompt, response_model,
max_retries=3)`: calls `structured_call_with_usage` via
`asyncio.to_thread`, emits `nthlayer.llm.call` OTel event via
`emit_llm_event` (provider from `model.split("/")`,
`caller="respond.{role.value}"`), returns
`result.data.model_dump_json()` so existing `parse_response` (JSON
string → dataclass) is reused without rewrite.

`_emit_verdict` accepts `metadata: dict | None = None` kwarg forwarded
to `verdict_create` unchanged — subclasses attach role-specific
structured fields (e.g. `RemediationAgent` populates
`metadata.custom.proposed_action` / `target` so bench brief reads
structured fields without parsing free-form strings).

Per-agent subclasses (`triage.py`, `investigation.py`,
`communication.py`, `remediation.py`):

- **TriageAgent**: `role=TRIAGE`, `default_timeout=15`,
  `response_model=TriageResponse` (P3-E.2 structured call path).
  `build_prompt` uses `_build_service_context_prompt`, prunes
  topology to trigger service + 1 hop. `parse_response` accepts both
  `assigned_team` and `team_assignment` field names, clamps severity
  0-4. `_post_execute` hook: when any trigger verdict has tag
  `agent_model_update` AND `result.severity <= 2`, calls
  `_request_autonomy_reduction(agent_name="triage",
  arbiter_url=config["arbiter_url"])`.
- **InvestigationAgent**: `role=INVESTIGATION`, `default_timeout=60`,
  `response_model=InvestigationResponse` (P3-E.2). Reads
  nthlayer-correlate correlation verdicts from verdict store via
  `trigger_verdict_ids`. Topology pruned to blast radius + 1 hop.
  `parse_response` accepts both `description` and `hypothesis` field
  names for hypotheses. Mechanical threshold check clears
  `root_cause` when `root_cause_confidence < threshold` (config
  `root_cause_threshold`, default 0.7).
- **CommunicationAgent**: `role=COMMUNICATION`, `default_timeout=20`,
  `response_model=CommunicationResponse` (P3-E.2). Two-phase: Phase 1
  (remediation is None) drafts initial status update; Phase 2
  (remediation set) drafts resolution update. `parse_response`
  accepts both `updates` and `messages` arrays, synthesises flat
  update from `title` / `impact_description` / `current_status` /
  `summary` / `message` when no structured array. `_apply_result`
  appends Phase 2 updates (`.extend`) rather than replacing Phase 1.
- **RemediationAgent**: `role=REMEDIATION`, `default_timeout=30`,
  `response_model=RemediationResponse` (P3-E.2). Constructor accepts
  `safe_action_registry: SafeActionRegistry | None` (None = worker
  mode pending P3-E.3). **Approval ratchet** —
  `registry.requires_approval=True` can never be downgraded by model.
  Rejects hallucinated actions (not in registry) →
  `proposed_action=None` + `requires_human_approval=True`.
  None-registry path (opensrm-saun.1.3): when registry is None,
  `parse_response` preserves `proposed_action` but forces
  `requires_human_approval=True` + logs warning. `_post_execute`
  guard (`self._registry is not None`) prevents AttributeError.
  `parse_response` accepts `proposed_action` / `recommended_action` /
  `action` and `target` / `target_service` field name aliases.
  `_build_metadata(result) -> {"custom": {"proposed_action":
  result.proposed_action, "target": result.target}}` (both keys
  always present — None disambiguates rejected/missing from absent).
  `_build_degraded_metadata() -> {"custom": {"proposed_action":
  None, "target": None}}` (explicit None so brief renders "manual
  intervention required" without inferring degradation from absence).
  Bead 1 (structured remediation emission): these 4 sites emit
  structured fields read by bench brief for `recommended_action` /
  `recommended_target`.

`notification_backends/slack_backend.py` — `SlackNotificationBackend`:
`send()` (DM) + `send_to_channel()` (channel post with `@here`).
Threading (opensrm-st4s.4): `_thread_anchors: dict[tuple[incident_id,
channel], str]` tracks first successful `message_ts` per
`(incident_id, channel)`. Subsequent sends pass stored ts as
`thread_ts`. DM thread key=`(incident_id, slack_id)`, channel thread
key=`(incident_id, channel)` — independent. `@here` suppressed on
thread replies (`include_at_here = thread_ts is None`, so re-paging
doesn't defeat threading). Failed first-send does not anchor (next
attempt restarts top-level). `_build_incident_blocks(payload, *,
include_at_here=False)` builds Block Kit. `SEVERITY_EMOJI` lookup
dict `{1→red, 2→orange, 3→yellow, 4→blue}`.

`oncall/runner.py` — `EscalationRunner`: `start_escalation` → fires
due steps immediately → background `_run_loop`. `_execute_step`
routes all `backend.send` / `send_to_channel` calls through
`_safe_send` wrapper (opensrm-st4s.4) which catches any uncaught
exception and converts to `NotificationResult(delivered=False,
error="<exc_type>: <msg>")` — belt-and-braces so a future backend bug
cannot kill the escalation step. `_safe_send(incident_id, channel,
recipient_name, send_fn, *args)`. `acknowledge()` cancels background
task. `slack_channel` step uses `backends["slack_dm"]` for
`send_to_channel` (channel name resolved from `_slack_channel`).

## `learn/` — LLM-powered retrospective + calibration + retention

`cli.py` — `nthlayer-learn` CLI: `accuracy` / `list` /
`retrospective`. Imports re-pointed to
`nthlayer_common.verdicts.{serialise, sqlite_store, store}` (was
`nthlayer_learn.*`; silently broken until opensrm-jmy.2 RM.7 fallout
fix).

`retrospective.py` — Walks verdict lineage to produce post-incident
analysis. Imports re-pointed to `nthlayer_common.verdicts.{core,
models, store}` (was `nthlayer_learn.*`; silently broken until
opensrm-jmy.2 RM.7 fallout fix). `_compute_financial_impact`
(opensrm-jmy.1): refactored to call
`nthlayer_common.outcomes.compute_financial_impact` +
`estimate_decisions_in_window`. Metric path uses
`breach_counts_by_service[svc]` as `decisions_affected`
(`volume_source="metric"`); spec_estimate fallback prorates from
`estimated_daily_decisions` over incident window
(`volume_source="spec_estimate"`). `blast_radius` accepts both str
list and `[{"service": ...}]` dict list. Multi-service aggregation:
iterates blast_radius, accumulates per-service `FinancialImpact`,
returns aggregate dict `{estimated, currency, decisions_affected,
failure_mode, volume_source}` or None when no service has an
outcomes block. Duplicate manifests per service deduplicated by
`metadata.name`.

`_load_manifests_from_specs` (opensrm-jmy.21) returns
`LoadedManifests(manifests, parse_failures)` over both `*.yaml` and
`*.yml`, sorted. `manifests` feeds `declared_dependencies_by_service`
(`_extract_declared_dependencies`, opensrm-jmy.21) and
`_compute_financial_impact`; `parse_failures` reaches callers as
`retro.metadata.custom["manifest_parse_failures"]` — always present, 0
on a clean load — and as the CLI's "Manifest parse failures: N" line
(opensrm-oh27). A file that fails to load is counted only when
`_foreign_yaml_reason` finds evidence it was aiming at being a
manifest; foreign YAML sharing the directory is dropped with a
`manifest_file_ignored` debug log carrying the reason. Parse failures
log `manifest_parse_failed` with `spec_file` and `error`.

`recommendations.py` — NEW (opensrm-jmy.2): `SpecRecommendation` +
`Recommendation` dataclasses + `analyze_incident()` glue.
`Recommendation` fields: service / type / rationale / proposed_value
/ confidence(0.0) / path / current_value / financial_impact /
evidence([]). Attribute named `field` uses `import dataclasses` +
`dataclasses.field()` to avoid collision with `dataclasses.field`.
`SpecRecommendation.requires_human_review` hardcoded True (raises
ValueError if False per spec §2). Confidence bounded 0.0-1.0.
`to_yaml() -> {apiVersion: opensrm.io/v1, kind: SpecRecommendation,
...}` dropping empty optional fields.

Two heuristics:

- `tighten_slo`: judgment SLO breach gap >80% of error budget →
  mid-point blend between observed and target at confidence 0.7;
  skipped for classical SLOs and `target=100`.
- `add_deploy_gate`: change-shaped root causes `{deploy,
  model_deploy, config_change, model_regression}` → gate proposal at
  confidence 0.65, or operator-fill placeholder at 0.4.

Constants: `_TIGHTEN_SLO_GAP_FRACTION=0.80`, `_TIGHTEN_SLO_BLEND=0.5`.
Full `--apply-to` / `--pr` / spec patch generation deferred to
opensrm-jmy.6.

`worker.py` — P3-F.1 splits into two WorkerModule impls:

- `LearnOutcomeModule` (60s): polls core for pending verdicts (batch
  50, oldest first), attempts resolution via five paths:
  1. lineage: downstream verdict references this one.
  2. calibration_sampling: ground-truth label exists.
  3. downstream_signal: external event attributed via `parent_ids`.
  4. score_outcome_divergence: confidence vs observed outcome.
  5. expiry: >7d pending → "expired".

  Emits `calibration_signal` assessment on resolution (NOT on expiry
  — absence of signal is not a quality signal). `calibration_signal`
  data: `{verdict_id, verdict_type, expressed_confidence,
  observed_outcome, calibration_delta, resolution_path,
  producer_system}`. Expiry threshold configurable via
  `workers.learn.expiry_threshold_days` (default 7).
- `LearnRetrospectiveModule` (30s): cursor-based poll for new
  `correlation_snapshot` assessments (same pattern as correlate),
  wraps existing `build_retrospective()` swapping VerdictStore reads
  for `CoreAPIClient` calls. Retrospective data:
  `{correlation_snapshot_id, duration_minutes, decisions_affected,
  verdict_count, root_cause, blast_radius, timeline[:20],
  recommendations, outcome_coverage{resolved, pending, total}}` —
  `outcome_coverage` makes incomplete-outcome retrospectives
  transparent.

Both assessment kinds (`retrospective`, `calibration_signal`) added
to `ASSESSMENT_KINDS` in `nthlayer_common/cloudevents.py` [P3-F.1].

## YAML data contracts

```
src/prompts/         # YAML prompt data-contract files
                     # (name/version/system/response_schema; no logic
                     # in YAML — schema defined once, used in prompt
                     # loader and parser)
  triage.yaml        # severity 0-4, blast_radius[], affected_slos[],
                     # assigned_team, confidence
  communication.yaml
  investigation.yaml
  remediation.yaml
  reasoning.yaml
  snapshot.yaml
  evaluator.yaml     # ModelEvaluator prompt (P3-C.3): dimensions,
                     # agent_name, task_id, output_content. Loaded via
                     # load_prompt() 4 levels up from
                     # pipeline/evaluator.py.

src/registry/
  safe-actions.yaml  # Safe action policy: 5 actions each with risk /
                     # requires_approval / cooldown_seconds /
                     # target_type / applicable_to / not_applicable_to /
                     # blast_radius / estimated_recovery / binding:
                     #   rollback (high, requires_approval, ArgoCD
                     #     webhook binding)
                     #   scale_up (low, no approval, stub, not
                     #     applicable to ai-gate — AI gate failures are
                     #     judgment quality issues not capacity)
                     #   disable_feature_flag (medium, requires_approval,
                     #     stub)
                     #   reduce_autonomy (low, no approval,
                     #     target_type=agent, ai-gate only, stub —
                     #     autonomy ratchet is one-way safe)
                     #   pause_pipeline (medium, requires_approval, stub)
                     # Enforcement logic (novel action rejection,
                     # approval ratchet, applicability checks) stays in
                     # Python — this file is policy only.
```

## Tests (1873 passed, 1 skipped baseline)

Test layout broadly mirrors `src/` per-module:

```
tests/
  test_runner.py                 # ModuleRunner tests
  release-smoke/                 # Walks every module via pkgutil,
                                 # asserts every __all__ resolves
  observe/
    test_observe_worker.py       # All three modules + helpers
    test_gate_adapter.py
    test_slo_collector.py
    test_store.py
    test_assessment.py
    test_decision_records.py
    test_cli_decision_records.py
    test_explanation.py          # 22 tests inc. drift causes + cold-start
  scenarios/synthetic/           # 12 respond scenario YAML fixtures
  measure/
    test_evaluator.py            # P3-C.3 ModelEvaluator
    test_measure_worker.py       # Severity-based autonomy ratchet etc.
  correlate/
    scenarios/synthetic/         # 5 correlate scenarios (co-located
                                 # to avoid filename collision with
                                 # respond's set)
    test_session.py
    test_correlate_worker.py
    test_topology_module.py
    test_contract_module.py
    test_summary.py              # P3-D.3 NL summary
    test_snapshot_model.py       # opensrm-saun.1.2.1 Assessment
                                 # emission
    test_correlate_command.py
    test_reasoning.py
  respond/
    test_agent_base.py
    test_remediation.py
    test_config.py
    test_coordinator.py
    test_respond_worker.py       # P3-E.1
    test_respond_worker_integration.py  # End-to-end milestone
    test_runner.py               # EscalationRunner
    test_schedule.py             # resolve_oncall (st4s.5)
  learn/
    test_learn_worker.py         # Both LearnOutcomeModule +
                                 # LearnRetrospectiveModule
    test_recommendations.py      # 22 tests, opensrm-jmy.2
    test_retrospective_financial.py  # 10 tests, opensrm-jmy.1
```

(Full per-test inventory is in the test files themselves — running
`pytest --collect-only` is faster than maintaining a static list.)

## Runtime dependencies

- `nthlayer-common>=0.1.8` (editable local, path
  `../nthlayer-common`) — shared utilities, verdict model, decision
  records, LLM wrapper, CoreAPIClient.
- `httpx>=0.27` — HTTP client.
- `pyyaml>=6.0` — YAML parsing.
- `structlog>=24.1.0` — structured logging.
- `scipy>=1.11`, `numpy>=1.24` — statistical analysis
  (measure / correlate).
- `starlette>=0.40`, `uvicorn>=0.30` — ASGI server (respond
  `ApprovalServer`).
- `python-multipart>=0.0.18` — required by `ApprovalServer`'s Slack
  interaction endpoint (`respond/server.py` — `await request.form()`
  hard-imports `python-multipart` via Starlette). **Production dep**
  (not dev-only) because operators running with
  `slack_signing_secret` set need it at runtime.

Dev: `pytest>=8.2`, `pytest-asyncio>=0.23`
(`asyncio_mode = "auto"`), `respx>=0.21` (HTTP mocking), `ruff>=0.8`.

`pyproject.toml` is authoritative.
