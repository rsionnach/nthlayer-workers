# Changelog — nthlayer-workers (Tier 2)

This file narrates the build sequence behind the initial state of this repository,
in prose. The repository was created from working code that had been developed
across the ecosystem under the v1.5 epic plan; we did not reconstruct phase-by-phase
git history because that history did not exist as commits at the time the work
was being done. This narrative is the honest substitute.

## [1.6.0](https://github.com/rsionnach/nthlayer-workers/compare/v1.5.0...v1.6.0) (2026-05-10)


### Features

* **correlate:** add confidence field to SnapshotSummary (P3-D.3) ([25a3481](https://github.com/rsionnach/nthlayer-workers/commit/25a348110a7d09d9cc785f5289665562b4af38db))
* **correlate:** migrate cold-path CLI from pseudo-verdicts to assessments (opensrm-saun.1.2.1) ([14a46a7](https://github.com/rsionnach/nthlayer-workers/commit/14a46a77dd05436d19b539a18241fe1ad442b099))
* **correlate:** verdict chain correlation analyzer (opensrm-jmy.3) ([5b2a5df](https://github.com/rsionnach/nthlayer-workers/commit/5b2a5df445453ad847428a302b50b6d9c448ca7c))
* **explain:** enrich budget causes with drift assessment data ([57a767d](https://github.com/rsionnach/nthlayer-workers/commit/57a767d7c96d988fa89de1e59d3b6e8249143519))
* **learn:** SpecRecommendation engine — Learn → Spec MVP (opensrm-jmy.2) ([c6f0d94](https://github.com/rsionnach/nthlayer-workers/commit/c6f0d94d8f2bd59bff41831406025ca7c44cd248))
* **learn:** wire spec § 1 financial impact into retrospectives (opensrm-jmy.1) ([2e3312d](https://github.com/rsionnach/nthlayer-workers/commit/2e3312db32e121c19b50253bde2ca7c182bd570b))
* **respond:** eager case creation + saun.1.2 cleanup ([0aede6e](https://github.com/rsionnach/nthlayer-workers/commit/0aede6e407299e96ec03fd4abcf5844f1df196b4))
* **respond:** Instructor-backed structured agent calls (P3-E.2) ([0a52c11](https://github.com/rsionnach/nthlayer-workers/commit/0a52c116a69fca8f8648a034a6779f22b62203b8))
* **respond:** Slack threading + backend-failure isolation (P3-E.4) ([3fe0b4a](https://github.com/rsionnach/nthlayer-workers/commit/3fe0b4a9d8e1c1489c59958a5408637664f92b62))
* **workers:** saun.1.2 wire-format alignment across all worker modules ([f998699](https://github.com/rsionnach/nthlayer-workers/commit/f998699e2319fbfc20af975bae7a7e67553cddaf))


### Bug Fixes

* **respond/remediation:** None-registry safety guard (opensrm-saun.1.3) ([9e770a2](https://github.com/rsionnach/nthlayer-workers/commit/9e770a280174fa8993a0f8c245cff40057785168))
* **respond:** emit OTel event on structured-call failure (R5 follow-up) ([2f436c5](https://github.com/rsionnach/nthlayer-workers/commit/2f436c5e9fe21bc9107e6f1f3fbbad85f7b4c332))
* **respond:** replace stale ../nthlayer-correlate path in error message ([69221da](https://github.com/rsionnach/nthlayer-workers/commit/69221da27e30f5de53d0c766a9bad995820aee3c))
* **respond:** set verdict_type on emitted verdicts (opensrm-saun.1.2) ([8d53e13](https://github.com/rsionnach/nthlayer-workers/commit/8d53e130c504566b0f7257f39d402335364e42f1))
* **safe-actions:** SSRF allowlist, injection guard, response opacity ([cd4330b](https://github.com/rsionnach/nthlayer-workers/commit/cd4330bda77d94b7090c589b7d91d1d468d83911))
* **workers:** handle None from get_sli_value as no-data ([7bbcad8](https://github.com/rsionnach/nthlayer-workers/commit/7bbcad8b02646fa067bb9e839c5191f8a1b62f3f))


### Code Refactoring

* **measure:** adopt 0-100 percentage canonical convention ([713faeb](https://github.com/rsionnach/nthlayer-workers/commit/713faeb45fe0d0509e9c390c8f9676d694825418))


### Documentation

* add README — Tier 2 consolidated worker runtime overview ([548355f](https://github.com/rsionnach/nthlayer-workers/commit/548355fbfcfef66d4289638b8f7455d33aa59c01))
* **CLAUDE.md:** catalogue retrospective financial impact wiring (opensrm-jmy.1) ([a1f0a28](https://github.com/rsionnach/nthlayer-workers/commit/a1f0a28e0e8ee030d56d255e1cc79e119c27104a))
* **CLAUDE.md:** document drift-enriched ExplanationEngine ([0cd1ee2](https://github.com/rsionnach/nthlayer-workers/commit/0cd1ee26dbb77c70ee07a207a43e7509b4352641))
* **CLAUDE.md:** document Instructor-backed agent structure ([ade2f46](https://github.com/rsionnach/nthlayer-workers/commit/ade2f461f1e1894e8d72d1bd083a0e0f8fdcdca8))
* **CLAUDE.md:** document opensrm-5fff.1 percentage convention in measure entries ([3be5fcc](https://github.com/rsionnach/nthlayer-workers/commit/3be5fcc0455b2ab8b351ffb36e499be278baf5f1))
* **CLAUDE.md:** document Slack threading + _safe_send dispatcher wrapper ([4f72d5c](https://github.com/rsionnach/nthlayer-workers/commit/4f72d5c87685dbd1fd8f3382d700b01a37e43e66))
* **CLAUDE.md:** document SnapshotSummary confidence field + tests ([a214c47](https://github.com/rsionnach/nthlayer-workers/commit/a214c47d4ba02efc53dba6aaee786ef7d2556bdd))
* **CLAUDE.md:** document SpecRecommendation engine + RM.7 import fix ([052a64e](https://github.com/rsionnach/nthlayer-workers/commit/052a64e8721c142fe321953d8c989f85586243f8))
* **CLAUDE.md:** document st4s.5 on-call test coverage ([c63fef7](https://github.com/rsionnach/nthlayer-workers/commit/c63fef7628946c195b931f798824ff03d25dc79c))
* **CLAUDE.md:** document test_collect_none_sli_yields_no_data ([a98f07b](https://github.com/rsionnach/nthlayer-workers/commit/a98f07ba909c3667409904197ab1bbd5f75e7091))
* **CLAUDE.md:** expand test_explanation.py inventory entry ([8231265](https://github.com/rsionnach/nthlayer-workers/commit/82312657dbcab16f6bfecdb882635b42028f9565))
* **comments:** inline pointers to saun.1.2 decision corpus ([6370bcd](https://github.com/rsionnach/nthlayer-workers/commit/6370bcdf23fdf40b3737a2fac79abd7754779cbf))

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
