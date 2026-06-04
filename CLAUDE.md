# nthlayer-workers

Unified Tier 2 background computation. Houses all worker modules:
observe, measure, correlate, respond, learn. Talks to nthlayer-core
exclusively via HTTP — **never accesses the SQLite store directly**.

## Stack

Python ≥3.11, `uv`-managed.

## Build / test / lint / run commands

→ See `AGENTS.md`.

## Hard rules

These are load-bearing — wrong-side mistakes either break the worker
contract with core, silently lose state across restarts, fork the
verdict chain, or make module composition impossible.

1. **HTTP-only to nthlayer-core.** No worker opens the SQLite store
   directly. All reads/writes go through `CoreAPIClient`. If you need
   state not exposed by the core API, add a core endpoint first.

2. **One module = one responsibility. No internal timers.** Each
   `WorkerModule` impl runs on the cadence its `ModuleRunner`
   registration specifies — do not start an `asyncio` background task
   inside `process_cycle()` that fires "every N seconds." If you need
   a different cadence, register a separate module at that cadence.
   This is why observe / correlate were both split into 3 modules at
   P3-B.2 / P3-D.2.

3. **`component_state` is the only crash-recovery primitive.**
   `restore_state` accepts the prior persisted blob (or None on first
   cycle / API failure); `get_state` returns what to persist after a
   successful cycle. Anything not in the persisted blob is lost on
   restart. The `RespondModule` worker state must round-trip
   `verdict_chain` — missing it breaks lineage continuity post-restart
   (`parent_ids=None` on next-step verdicts). Pinned by
   `TestStatePersistence::test_state_roundtrip_preserves_all_fields`.

4. **Verdict data model lives in `nthlayer_common.verdicts`.**
   `learn/` only adds analytical operations on top. Do not duplicate
   the model here.

5. **Decision records use `nthlayer_common.records.SQLiteDecisionRecordStore`.**
   The content-addressed audit trail is shared across the ecosystem;
   the bridge in `observe/decision_records.py` is the only adapter
   that translates legacy `Assessment` → `records.Assessment`.

6. **Align internal types with canonical shapes; do not write
   adapters.** Adapters are for code you don't own.
   `ServiceSLO(service: str, slo: SLODefinition)` in `observe/slo/`
   carries the one field the common `SLODefinition` legitimately
   omits — it is not an adapter, just an additive carrier.
   `spec_loader.py` uses `nthlayer_common.manifest.load_manifest()`
   (canonical parser); the local `SLODefinition` was deleted in
   P3-B.1.

7. **Severity classification is deterministic. No LLM in v1.5.**
   Severity per judgment SLO type via `measure/worker.py`
   `classify_severity()` — budget-consumption types use
   `<200% / 200-500% / >500%` buckets; stability is binary; segments
   variance-based; calibration delta-based. Do not add an LLM call to
   this path "to improve accuracy" — the governance layer is
   load-bearing and must be auditable.

8. **Autonomy is a one-way ratchet.** Five levels: fully_autonomous →
   autonomous → limited_autonomous → advisor → observer. Reductions:
   low = 1 level, high = 2 levels, critical → observer. `_autonomy_levels`
   is **NOT cleared on HEALTHY recovery** — re-breach continues from
   the previously reduced level. Pinned by
   `TestRebreachAfterRecovery`.

9. **SLO target convention is 0-100 percentage.** Same as
   `nthlayer-common` (see its CLAUDE.md). `MeasureModule._evaluate_slo`
   scales the Prometheus SLI (always 0.0-1.0 ratio) to percentage via
   `current_pct = current_value * 100` before comparing to the target.
   `_classify_budget_consumption` uses `error_budget_pct = 100.0 -
   target`. Guard `if error_budget_pct <= 0: return "critical"` — a
   `target=100` means zero budget.

10. **Approval ratchet on safe actions.**
    `registry.requires_approval=True` can never be downgraded by the
    model. Hallucinated actions (not in registry) → `proposed_action=None`
    + `requires_human_approval=True` + warning logged. The
    None-registry path (worker mode pending P3-E.3) preserves the
    proposal but forces `requires_human_approval=True`.

11. **YAML prompt files are data contracts only — no logic.** Schema
    defined once, used in prompt loader and parser
    (`load_prompt(path)` → `render_user_prompt(template, **kwargs)`).
    Confidence default 0.0. Same rule as the rest of the ecosystem
    (see `feedback_yaml_prompts`).

12. **Lint floor frozen.** Ruff `select = ["E4", "E7", "E9", "F",
    "I", "UP", "SIM", "B"]` post-opensrm-po23. `E501` and the full
    `W` family are separate hygiene calls, not part of the floor.
    See `AGENTS.md` for the canonical pin.

13. **Verdict submission is fail-open + structured failure metric.**
    `verdict_submission.submit_verdict_to_core(client, verdict, *,
    deployment_id) -> bool` never raises. On failure: logged +
    `errors_total(component="respond",
    error_type="verdict_submit").inc()` + returns False so the caller
    skips appending to `verdict_chain` (keeps in-memory chain
    consistent with core lineage, avoids dangling `parent_ids`).

14. **Test discipline.** Tests use real `Store(tmp_path)`, not stubs
    (see `feedback_shared_db_test`). Assertions on structured-data
    primitives (response fields, dataclass shape, enum values), not
    captured strings. Tests pin behaviour at boundaries — opensrm-y7dd
    R5 Pass 3 surfaced that `_judgment_slo_query` uses stdlib logger
    (kwargs would TypeError under structlog) as a load-bearing fact.

## Where to find detail

- Source layout, per-module design decisions, test-suite
  cross-reference: `docs/architecture.md`.
- Build / test / lint / run / CI / release: `AGENTS.md`.
- Verdict / assessment / record models the workers consume:
  `nthlayer-common/docs/architecture.md`.
- LLM provider routing, env vars, canned-LLM stub:
  `nthlayer-common/docs/llm-interface.md`.
- Core HTTP API the workers depend on:
  `nthlayer-core/docs/architecture.md`.
- README: project-level overview (`README.md`).
- Beads: `cd opensrm && bd ready --json`.
