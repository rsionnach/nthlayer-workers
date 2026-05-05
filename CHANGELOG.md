# Changelog — nthlayer-workers (Tier 2)

This file narrates the build sequence behind the initial state of this repository,
in prose. The repository was created from working code that had been developed
across the ecosystem under the v1.5 epic plan; we did not reconstruct phase-by-phase
git history because that history did not exist as commits at the time the work
was being done. This narrative is the honest substitute.

## v1.5.0 — 2026-05-03

First lockstep release with the rest of the v1.5 ecosystem. Phase 5
landed several substantive changes:

**Envelope transport on all 14 submission sites** (opensrm-saun.1.2).
Workers now wrap every verdict and assessment in a CloudEvents v1.0
envelope before submitting to core. Previously workers built envelopes
but stripped them at the wire — a half-implementation discovered by the
saun.1 three-tier integration test. Affected sites: respond ×3, measure
×3, observe ×4, correlate ×3, learn ×2, observe-gate CLI ×1 (note:
respond/verdict_submission removed the dead-code envelope strip).

**`verdict_type` typed column populated at 7 verdict_create sites**
(opensrm-saun.1.2). The Verdict dataclass's `verdict_type` column was
nullable and unset by every emitter; core's `GET /verdicts?type=...`
filter therefore couldn't find verdicts by their domain type. Fix routes
the role label through `verdict_type` at emission time. respond agents
emit `triage` / `investigation` / `communication` / `remediation` per
role; respond's coordinator approve+deny paths emit `remediation` (v1.5
owns the bundled flow; RBAC §10's separate approval/denial typing is a
v2 concept). measure/cli emits `quality_breach` on breach;
measure/tiering/promotion emits `autonomy_change`; bench's
reasoning_capture emits `operator_note`.

**Eager case creation in respond worker** (opensrm-saun.1.2). When respond
opens an `IncidentContext` (from a snapshot or fallback breach), it now
POSTs a case to core concurrently with the incident-open transition,
before triage runs. Operators see incidents in the bench queue
immediately. Case fields: `kind="incident"`, `underlying_verdict` anchored
on the breach verdict (snapshot path: from `data.parent_ids`; fallback
path: `breach["id"]`), `service`, `blast_radius` (env string for core's
`_derive_priority`), `has_active_incident=True`, `briefing` composed from
breach data. Failure non-fatal — case-create errors are logged but the
incident still progresses through the agent pipeline. correlate also
now sets `snapshot_data["parent_ids"]` to the list of QUALITY_SCORE event
verdict ids so respond can read them for the case anchor.

**RemediationAgent hardening against None safe_action_registry**
(opensrm-saun.1.3). Worker mode constructs `RemediationAgent` with
`safe_action_registry=None` (P3-E.3 wires the real registry). Pre-fix,
`parse_response` raised `AttributeError: 'NoneType' object has no attribute
'get'` on every cycle when the canned LLM stub proposed a real action.
Caught by the broad except in `AgentBase.execute`, but every remediation
hit the degraded path. Fix: constructor now accepts `SafeActionRegistry
| None`; when None, `parse_response` logs a warning, forces
`requires_human_approval=True`, and preserves the model's proposal for the
operator to review. `_post_execute` gains a defensive `is not None` guard
on the auto-execute branch.

**Cold-path correlation pseudo-verdicts migrated to assessments**
(opensrm-saun.1.2.1). Six call sites in `correlate/snapshot/model.py`
(×4), `correlate/cli.py`, and `respond/cli.py` were emitting
`verdict_create()` with `subject.type="correlation"` — but
`correlation_snapshot` is in `ASSESSMENT_KINDS`, not `VALID_VERDICT_TYPES`.
The hot-path correlate worker already emitted these correctly as
assessments; this bead aligned the cold-path CLI sites with the same
primitive. `ModelInterface.interpret()` returns `list[Assessment]`
(typed); `correlate_command` writes via `SQLiteAssessmentStore` (sharing
the verdict store's db file). Decision-record write integration deferred
— no `write_decision_assessment` helper exists yet in
`nthlayer-common.records`. Decision-record + Slack notification paths
in `correlate/cli.py` are routed to no-ops with anchoring comments
explaining the maintenance-mode posture.

## Provenance

`nthlayer-workers` is the Tier 2 (background computation) process in the three-tier
NthLayer architecture decided 2026-04-21
([`docs/superpowers/specs/2026-04-21-spec-revision-summary.md`][spec-revision] in the
`opensrm` repo). It is one of the three new repositories created as part of the
six-repo consolidation
([`docs/superpowers/specs/2026-04-21-repo-consolidation-recommendation.md`][consol]).

This single process houses the five worker modules that were previously developed
in five separate repos (`nthlayer-observe`, `nthlayer-measure`, `nthlayer-correlate`,
`nthlayer-respond`, `nthlayer-learn`). Those legacy repos are being deprecated
in favour of this consolidated tree. Forward-port verification was completed
before deprecation
(see `opensrm` repo for the audit record).

Communicates with `nthlayer-core` exclusively via HTTP API — never reads or
writes core's SQLite stores directly. This is the v1.5 boundary that makes
the three-tier model honest.

## Build sequence (epic-level)

The contents of this initial commit reflect work from the **v1.5 epic plan**
([`docs/superpowers/plans/2026-04-21-nthlayer-v1.5-epic-tree.md`][v15-plan]),
phase 3 in particular:

### Phase 3A — module runner

`runner.py` defines the `WorkerModule` protocol (`name`, `restore_state`,
`process_cycle`, `get_state`) and `ModuleRunner` orchestrator. State is
restored from core via `CoreAPIClient.get_component_state(name)` on startup
and persisted via `put_component_state` after each cycle. Heartbeats emit
once per tick where any module ran. Graceful SIGTERM/SIGINT shutdown
finishes the current cycle, persists state, and exits.

### Phase 3B — observe module

Three `WorkerModule` implementations split by cadence:

- `ObserveCollectModule` (60s): polls manifests from core, queries Prometheus
  for SLO indicators, submits `slo_status` assessments + a `portfolio_status`
  rollup with `parent_ids` referencing the contributing slo_status assessment IDs.
- `ObserveDriftModule` (1800s): drift analysis per (service, tier, slo_name,
  window) — emits `drift_signal` assessments.
- `ObserveTopologyModule` (86400s): dependency discovery + per-service blast
  radius calculation — emits `dependency_graph` assessments.

CLI side: `nthlayer-observe` retains 10 commands (collect, drift, verify,
discover, dependencies, blast-radius, portfolio, scorecard, check-deploy,
explain) for local dev use. `nthlayer-workers gate` is the CLI deploy gate
(no HTTP API in v1.5).

### Phase 3C — measure module

`MeasureModule` (60s) — judgment SLO evaluation. Fetches manifests from core,
queries Prometheus per judgment SLO, submits `judgment_slo_evaluation` assessments,
detects HEALTHY→BREACH transitions, emits `quality_breach` verdicts with
severity (low/high/critical) and `autonomy_change` verdicts when governance
reduces autonomy. Five autonomy levels with severity-based reduction rules.

`pipeline/evaluator.py` implements `ModelEvaluator` — Instructor-backed
LLM evaluator for agent output quality scoring. Cost-accounting OTel events.

### Phase 3D — correlate module

Three `WorkerModule` implementations split by responsibility:

- `CorrelateSessionModule` (10s): session-window-based event correlation. Polls
  verdicts/assessments from core, ingests into `SessionWindowManager`, emits
  `correlation_snapshot` assessments on window close (gap=60s, max_duration=15m,
  or `quality_breach` trigger). NL summary via Instructor (5s timeout, non-blocking).
- `CorrelateTopologyModule` (1h): topology drift detection from trace evidence
  vs declared dependencies. Emits `topology_drift` assessments.
- `CorrelateContractModule` (1h): contract divergence — promised vs observed
  availability/latency per service. Emits `contract_divergence` assessments.

### Phase 3E — respond module

`RespondModule` (30s) — incident response coordinator as a worker module.
Polls `correlation_snapshot` assessments (primary) and `quality_breach`
verdicts (fallback after threshold) from core. Drives incidents through the
4-step pipeline (triage → investigation+communication → remediation →
resolution-communication). Submits all verdicts to core via
`CoreAPIClient.submit_verdict()` with CloudEvents envelope. Persists incident
state to `component_state("respond")`. Per-step timeout (90s default) prevents
single-incident lockup. Capture-at-write-time escalation flag.

Design principles documented in
`opensrm/docs/superpowers/specs/2026-04-25-p3-e1-respond-coordinator-worker-design.md`:

- **Situation-shaped triggers, not signal-shaped** — primary trigger is
  `correlation_snapshot` (the situation), fallback to `quality_breach`
  (the signal) only when correlate is degraded.
- **Capture-at-write-time** — store decision-relevant fields on local
  in-process state when the agent has them, instead of re-fetching later.
- **Worker-mode invariant: validate state on entry** — polling-driven entry
  points lack the state-knowledge that operator-driven CLI invocation has.

R5 review (Correctness / Clarity / Edge Cases / Excellence) all PASS.

### Phase 3F — learn module

Two `WorkerModule` implementations:

- `LearnOutcomeModule` (60s): outcome resolution for pending verdicts via
  five paths (lineage / calibration sampling / downstream signal / score-outcome
  divergence / expiry). Emits `calibration_signal` assessments on resolution.
- `LearnRetrospectiveModule` (30s): cursor-based polling for new
  `correlation_snapshot` assessments; wraps existing `build_retrospective`
  to submit `retrospective` assessments (with outcome_coverage transparency).

### Code quality

All Phase 3 modules went through Rule of Five code review (Correctness,
Clarity, Edge Cases, Excellence). The most recent — P3-E.1 (respond) —
captured the discipline as a project convention; that R5 history is in
the relevant per-component review records in the `opensrm` Dolt DB.

## What is in this initial commit

- `src/nthlayer_workers/cli.py` — `nthlayer-workers serve` (registers all
  worker modules with their cadences) and `nthlayer-workers gate` (CLI-only
  deploy gate).
- `src/nthlayer_workers/runner.py` — `ModuleRunner` + `WorkerModule` protocol.
- `src/nthlayer_workers/{observe,measure,correlate,respond,learn}/` — five
  worker module trees.
- `tests/` — comprehensive test coverage per phase, plus integration tests
  for cross-module flows.

## Things deliberately NOT yet in this repo

- **Instructor-backed agent calls in respond.** P3-E.1 ships the structural
  migration (worker module shape, core-API verdict submission, component_state
  persistence). P3-E.2 swaps `llm_call` to `structured_call` with Instructor
  Pydantic models. Tracked as follow-up beads in the epic.
- **Safe-action execution in respond worker mode.** Legacy CLI path retains
  it; worker-mode AWAITING_APPROVAL incidents wait for P3-E.3 approval-verdict
  polling.
- **Notification backends + on-call escalation in respond worker mode.** P3-E.4
  and P3-E.5.
- **Cases API integration for incidents.** Currently respond uses a
  `component_state` blob for active incidents. Cases-API integration is a
  follow-up that brings queryability, lease ownership, and bench integration.
- **SRE operator commands** (oncall, brief, shift-report, suppress,
  post-incident, delegate). These are in the legacy `nthlayer-respond` repo
  on `feat/opensrm-0rg-cli` and are intentionally NOT ported here — they are
  operator-interactive commands that belong in `nthlayer-bench` (Tier 3).
  Inventory + bench-equivalent shape:
  `opensrm/docs/superpowers/specs/2026-04-26-respond-sre-cli-inventory-for-bench.md`.

## How this repo evolves

The five worker modules will continue to evolve in parallel. Major future work:

- **P3-E.2 / E.3 / E.4 / E.5** — respond agent Instructor migration,
  safe-action execution, notification backends, on-call escalation in worker mode.
- **P3-D.3** — correlate NL summaries (already implemented; future iterations
  on prompt design).
- **v1.5 → v2** — migration to IPLD CIDs, Bytewax dataflow option, authorise/
  executor moving into core. Will be developed on a feature branch.

[spec-revision]: ../opensrm/docs/superpowers/specs/2026-04-21-spec-revision-summary.md
[consol]: ../opensrm/docs/superpowers/specs/2026-04-21-repo-consolidation-recommendation.md
[v15-plan]: ../opensrm/docs/superpowers/plans/2026-04-21-nthlayer-v1.5-epic-tree.md
