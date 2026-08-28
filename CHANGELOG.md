# Changelog — nthlayer-workers (Tier 2)

This file narrates the build sequence behind the initial state of this repository,
in prose. The repository was created from working code that had been developed
across the ecosystem under the v1.5 epic plan; we did not reconstruct phase-by-phase
git history because that history did not exist as commits at the time the work
was being done. This narrative is the honest substitute.

## [2.0.0](https://github.com/rsionnach/nthlayer-workers/compare/v1.7.1...v2.0.0) (2026-08-28)


### ⚠ BREAKING CHANGES

* **observe:** observe.slo.spec_loader.load_specs now returns LoadedSpecs(service_slos, parse_failures) instead of list[ServiceSLO]. Callers iterating the return value directly must use `.service_slos`. The count exists so a caller can tell a partial view from a complete one: before this, a service whose manifest failed to parse contributed zero SLOs and was indistinguishable from one declaring none. Requires nthlayer-common>=2.1.2.

### Features

* **observe:** return LoadedSpecs from load_specs; require common&gt;=2.1.2 ([dedded3](https://github.com/rsionnach/nthlayer-workers/commit/dedded3fac1fa9475a9ca2b217f5189e3abdf06b))


### Bug Fixes

* **learn:** log and count manifest parse failures in retrospective (opensrm-oh27) ([375f667](https://github.com/rsionnach/nthlayer-workers/commit/375f667ac1469e27261d5572f7edfcb631f46323))
* **learn:** log and count manifest parse failures in retrospective (opensrm-oh27) ([4cee6d7](https://github.com/rsionnach/nthlayer-workers/commit/4cee6d77922f7aed8d9e8f0597c2d0aababae299))
* **measure:** judgment SLO breach check could never fire (opensrm-fxln) ([0da9b39](https://github.com/rsionnach/nthlayer-workers/commit/0da9b39e31ff38346d2a5e1ff0ee2886706d99ea))
* **measure:** load_specs must understand v2 manifests (opensrm-fxln) ([95c63c7](https://github.com/rsionnach/nthlayer-workers/commit/95c63c783b9b07265fc515f2f0ea3e6dcbc241ce))
* **measure:** query and breach rule are a matched pair (opensrm-fxln) ([bbe2c0c](https://github.com/rsionnach/nthlayer-workers/commit/bbe2c0caa826508b9c635915f6a8941651766883))
* **measure:** v2 manifests yielded zero SLOs and judgment SLOs could not breach (opensrm-fxln) ([718c5e6](https://github.com/rsionnach/nthlayer-workers/commit/718c5e6ac16e91fd3a7d3a6e759d713fb0a1814b))
* **observe:** log and count spec parse failures (opensrm-3470) ([5e5769f](https://github.com/rsionnach/nthlayer-workers/commit/5e5769fa77894849077fb48af00bf893a1d19e27))
* **observe:** log and count spec parse failures (opensrm-3470) ([4162edf](https://github.com/rsionnach/nthlayer-workers/commit/4162edf4dc39a9c091baf32645ad871340f41285))


### Code Refactoring

* **learn:** use the shared scan gate instead of a local copy (opensrm-3470) ([5c68d8f](https://github.com/rsionnach/nthlayer-workers/commit/5c68d8fba86f0722a3de61be98be67b096f8f47c))
* **learn:** use the shared scan gate instead of a local copy (opensrm-3470) ([e788c88](https://github.com/rsionnach/nthlayer-workers/commit/e788c88d2f7829eac517e4b0d6e5893a59dfffa1))
* **observe:** extract the parse-failure warning; rename a stale test (opensrm-3470) ([51cc2b2](https://github.com/rsionnach/nthlayer-workers/commit/51cc2b28d21105b58f7b5131d30e9e0572657ee9))
* **observe:** extract the parse-failure warning; rename a stale test (opensrm-3470) ([f320236](https://github.com/rsionnach/nthlayer-workers/commit/f320236a00881898666e78f64217d15c62b43cbe))


### Documentation

* add contributing guide (opensrm-tu04.4) ([866f155](https://github.com/rsionnach/nthlayer-workers/commit/866f155946e22ff198e60d5e0ae78fe74985227e))

## [1.7.1](https://github.com/rsionnach/nthlayer-workers/compare/v1.7.0...v1.7.1) (2026-06-24)


### Documentation

* link to ecosystem testing conventions (opensrm-2wkc) ([f549653](https://github.com/rsionnach/nthlayer-workers/commit/f5496535fda1f216ab6cfbe70d1b46589992b188))
* thin CLAUDE.md; move detail to AGENTS.md + docs/ ([47a3b2f](https://github.com/rsionnach/nthlayer-workers/commit/47a3b2f0ba9c2941fe2c08c1d88ec217999795c2))

## [1.7.0](https://github.com/rsionnach/nthlayer-workers/compare/v1.6.0...v1.7.0) (2026-06-03)


### Features

* **_apply:** apply_recommendations orchestration · opensrm-jmy.6 ([8ece5a3](https://github.com/rsionnach/nthlayer-workers/commit/8ece5a39997a03659d4cceb6d7279b363c5ed05a))
* **_apply:** manifest resolution helper · opensrm-jmy.6 ([9623030](https://github.com/rsionnach/nthlayer-workers/commit/9623030473f4030f6752043c2903678de4d47a9e))
* **_preview:** scalar preview generation · opensrm-jmy.6 ([801a9c5](https://github.com/rsionnach/nthlayer-workers/commit/801a9c5b004694056fdaacbc02eceae626cc9350))
* **_preview:** structural preview + drift marker tests · opensrm-jmy.6 ([4fae732](https://github.com/rsionnach/nthlayer-workers/commit/4fae732d80e205057841a0cee0f5aa8a64eccd37))
* **_yaml:** add apply_at_path with comment preservation · opensrm-jmy.6 ([80ee8b6](https://github.com/rsionnach/nthlayer-workers/commit/80ee8b6e9a9945d95557e616398908b686881c5f))
* **_yaml:** add normalize_scalar for type-tolerant comparison · opensrm-jmy.6 ([3eb0db5](https://github.com/rsionnach/nthlayer-workers/commit/3eb0db5add4ad6a377e95ecfaace486bfcedf39c))
* **_yaml:** add ruamel.yaml round-trip + resolve_path · opensrm-jmy.6 ([fb8cd6b](https://github.com/rsionnach/nthlayer-workers/commit/fb8cd6b9e9afbd22c2748e54c4cc3ff1795dae4e))
* **_yaml:** classify_outcome state machine · opensrm-jmy.6 ([8a8206c](https://github.com/rsionnach/nthlayer-workers/commit/8a8206c3789a533a60bee0f1443cf9cf81d43d16))
* **cli:** wire --pr path with branch + commit + push + gh · opensrm-jmy.6 ([1b43c0c](https://github.com/rsionnach/nthlayer-workers/commit/1b43c0c3387b5ea786897cf1a86ef5993354f9a1))
* **learn:** --include / --exclude flags on recommendations CLI · opensrm-jmy.24 ([baa40dc](https://github.com/rsionnach/nthlayer-workers/commit/baa40dc43142f2e86ac92613c45892f5cb4ab351))
* **learn:** --interactive TUI walkthrough · opensrm-jmy.22 ([024bfe2](https://github.com/rsionnach/nthlayer-workers/commit/024bfe24e6f38f1064ecf593cfae355b9939c20c))
* **learn:** --json output mode for recommendations CLI · opensrm-jmy.25 ([7332662](https://github.com/rsionnach/nthlayer-workers/commit/7332662ea9bb0ae580be8ed414aa255bf445f4aa))
* **learn:** add _gh.py pre-flight checks for jmy.6 (Task E1) ([ede132f](https://github.com/rsionnach/nthlayer-workers/commit/ede132f8326303bf1a1ea8e2f2e06a4d848fe0aa))
* **learn:** add create_pr_via_gh to _gh.py (jmy.6 E2) ([2b49def](https://github.com/rsionnach/nthlayer-workers/commit/2b49defbdc38a1acdde1357946aec7cfb65920bf))
* **learn:** add format_summary to _apply.py (jmy.6 D4) ([da915b8](https://github.com/rsionnach/nthlayer-workers/commit/da915b8a2724731b8f053ec3f3b021060d288343))
* **learn:** add recommendations subcommand + flag validation (jmy.6 F1) ([a80fc02](https://github.com/rsionnach/nthlayer-workers/commit/a80fc02f41bdba504522b6a7e37c721c3351821d))
* **learn:** add_dependency recommendation type · opensrm-jmy.21 ([4e36dd4](https://github.com/rsionnach/nthlayer-workers/commit/4e36dd4ca277178c707fa950dd12a7e3ad2c854d))
* **learn:** financial_impact on SpecRecommendation metadata · opensrm-jmy.23 ([c60ac17](https://github.com/rsionnach/nthlayer-workers/commit/c60ac17f41d537ed72ed2ed7f1ea51fd3692506e))
* **learn:** populate trigger_service on retrospective verdicts ([423ab36](https://github.com/rsionnach/nthlayer-workers/commit/423ab36ee40f4549e9a73ec823186def526ecfb3))
* **learn:** populate trigger_service on worker-path retrospectives ([5d764d6](https://github.com/rsionnach/nthlayer-workers/commit/5d764d6486150ed5ea129af57e325c1b1ad1cc3c))
* **learn:** resolve_trigger_service precedence helper ([4fc8ff3](https://github.com/rsionnach/nthlayer-workers/commit/4fc8ff318e3c89e7baca5de7cba81771a526c5e0))
* **learn:** wire _cmd_recommendations dispatch — F2 (jmy.6) ([f157362](https://github.com/rsionnach/nthlayer-workers/commit/f157362df47638e5ebac5d72571a95780fad390b))
* **recommendations:** add deterministic id field · opensrm-jmy.6 ([e0435cf](https://github.com/rsionnach/nthlayer-workers/commit/e0435cf5f984bb10f850f7f3f6b11e418db05982))
* **recommendations:** add OutcomeKind enum · opensrm-jmy.6 ([f725752](https://github.com/rsionnach/nthlayer-workers/commit/f725752a0e815553940dc83692b1d24d1b04b2ef))
* **recommendations:** add parse_plan_file for --from input · opensrm-jmy.6 ([ce03c53](https://github.com/rsionnach/nthlayer-workers/commit/ce03c53981f8f88817750af20d5b033194322a9a))
* **recommendations:** rename plan artefact to RecommendationPlan · opensrm-jmy.6 ([e8dd8f9](https://github.com/rsionnach/nthlayer-workers/commit/e8dd8f961d159ecd42bf96bc021e3d01bd740984))


### Bug Fixes

* **ci:** add tag-push + workflow_dispatch triggers to release.yml ([d65e97a](https://github.com/rsionnach/nthlayer-workers/commit/d65e97abd16028b3e593d1a4f652ed3bf220806b))
* **ci:** skip __main__ modules in smoke walker; move to release-smoke ([fc499e0](https://github.com/rsionnach/nthlayer-workers/commit/fc499e0172fd6110eb2a54794144ca9c96460a4c))
* guard worker __main__ entry points with if __name__ check ([b1f03ce](https://github.com/rsionnach/nthlayer-workers/commit/b1f03ce028bbe26fd185f252aeee6dcb91cd0b3d))
* **learn:** route ALREADY_APPLIED to skipped + idempotent exit_code · opensrm-1mja ([5248d6e](https://github.com/rsionnach/nthlayer-workers/commit/5248d6e91621f85c84d1992b228a01ad8889dbdd))
* **smoke:** skip legacy measure.api.server (no fastapi dep) ([bcfdbd2](https://github.com/rsionnach/nthlayer-workers/commit/bcfdbd2b2acdfe9781b9ec15e8bd433746f3edff))


### Code Refactoring

* read version from importlib.metadata, not source literal ([7df518f](https://github.com/rsionnach/nthlayer-workers/commit/7df518f83ac8e6f7b2f9929682a35413f3bf59f8))


### Documentation

* **CLAUDE.md:** document release-please + smoke gate + Dependabot ([c6bc45f](https://github.com/rsionnach/nthlayer-workers/commit/c6bc45ff3ac64a8513bf8620f0b6407e26a53fae))

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
