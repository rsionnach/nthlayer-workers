# nthlayer-workers

**Tier 2 of the [NthLayer](https://github.com/rsionnach/nthlayer) ecosystem.** Consolidated runtime process containing five worker modules: **observe**, **measure**, **correlate**, **respond**, **learn**. Communicates with [`nthlayer-core`](https://github.com/rsionnach/nthlayer-core) (Tier 1) exclusively via HTTP API — never accesses the SQLite store directly.

```bash
pip install nthlayer-workers
nthlayer-workers serve --core-url http://localhost:8000
```

## What it is

A single Python process running five cooperating modules behind a `ModuleRunner`. Each module has its own cycle interval, persists state to core's `component_state` for crash recovery, and submits verdicts and assessments to core via [`CoreAPIClient`](https://github.com/rsionnach/nthlayer-common). Worker failure is degradation, not outage.

- **Apache 2.0** licensed.
- **No direct DB access.** Everything goes through the core HTTP API.
- **Single-process, single-instance per core** in v1.5. Multi-instance HA is a v2 target.

## The five modules

### observe

Deterministic runtime infrastructure — SLO assessment, drift detection, topology discovery. No LLM. Three sub-modules registered separately with the runner:

| Module | Cycle | Output |
|---|---|---|
| `observe.collect` | 60 s | `slo_status` assessments + portfolio rollup |
| `observe.drift` | 1800 s | `drift_signal` assessments per (service, tier, slo, window) |
| `observe.topology` | 86400 s | `dependency_graph` assessment with blast-radius analysis |

Also ships a CLI-only **deploy gate**: `nthlayer-workers gate --service X` resolves the service tier, fetches the latest `slo_status`, runs `check_deploy()`, and submits a `deploy_gate` assessment. Exit codes: 0=APPROVED/WARNING, 1=eval error, 2=BLOCKED.

### measure

LLM-aware AI decision quality evaluation. One-way safety ratchet. 60 s cycle.

- Per judgment SLO: queries Prometheus, submits `judgment_slo_evaluation` assessment.
- On HEALTHY → BREACH transition: emits `quality_breach` verdict with severity (`low` / `high` / `critical`).
- Severity-based deterministic governance reduces autonomy (`fully_autonomous → autonomous → limited_autonomous → advisor → observer`); emits `autonomy_change` verdict if reduced.
- Autonomy is a **one-way ratchet** — never restored without explicit human approval.
- Quality scoring of agent outputs uses Instructor-backed structured LLM calls (`ModelEvaluator`).

### correlate

asyncio session-window correlation, topology drift, contract divergence. Three sub-modules:

| Module | Cycle | Output |
|---|---|---|
| `correlate.session` | 10 s | `correlation_snapshot` assessments — windows close on 60 s gap, 15 m max, or `quality_breach` trigger |
| `correlate.topology` | 3600 s | `topology_drift` assessment (Tempo backend optional) |
| `correlate.contract` | 3600 s | `contract_divergence` assessment per service whose observed SLI violates declared contract |

Snapshots include a non-blocking 5 s NL summary generated via Instructor (`structured_call`). Failures are logged and counted, never raised.

### respond

Multi-agent incident-response coordinator. 30 s cycle. Trigger ingestion is **situation-shaped**:

- **Primary trigger:** `correlation_snapshot` assessments (with cursor `snapshot_after`).
- **Fallback:** `quality_breach` verdicts older than `fallback_threshold_seconds` with no associated snapshot — used when correlate is degraded.

Pipeline: `[TRIAGE] → [INVESTIGATION, COMMUNICATION] → [REMEDIATION] → [COMMUNICATION]`. Each step is an Instructor-backed agent producing a verdict; `AWAITING_APPROVAL` is the worker-mode pause point (resumption is P3-E.3 work). Per-step `asyncio.wait_for(step_timeout_seconds)`. The first incident verdict's `parent_ids` are the trigger verdict IDs — that's the cross-module lineage bridge.

State (`{cursors, incidents}`) persisted to `component_state`. Terminal incidents pruned after 24 h.

### learn

LLM-powered retrospective + outcome resolution. Two sub-modules:

| Module | Cycle | Output |
|---|---|---|
| `learn.outcome` | 60 s | `calibration_signal` assessments — resolves pending verdicts via five paths: lineage, calibration sampling, downstream signal, score-outcome divergence, expiry |
| `learn.retrospective` | 30 s | `retrospective` assessments — cursor-based poll on new `correlation_snapshot`s |

Calibration signals fire only on real resolution, never on expiry — absence is not a quality signal.

## CLI

```bash
# Start the worker runtime (registers all modules)
nthlayer-workers serve \
  --core-url http://localhost:8000 \
  --instance-id worker-01 \
  --prometheus-url http://localhost:9090 \
  [--collect-interval 60] [--drift-interval 1800] [--topology-interval 86400] \
  [--correlate-interval 10] [--topology-drift-interval 3600] [--contract-interval 3600] \
  [--measure-interval 60] [--respond-interval 30] \
  [--outcome-interval 60] [--retrospective-interval 30] \
  [--expiry-threshold-days 7]

# Deploy gate (CLI-only in v1.5)
nthlayer-workers gate --service payment-api [--tier critical] [--commit-sha SHA] \
  [--core-url http://localhost:8000]

# Print version
nthlayer-workers -V
```

## How it talks to core

Every module is a `WorkerModule` Protocol implementation: `name`, `restore_state`, `process_cycle`, `get_state`. `ModuleRunner` orchestrates:

1. On startup: `restore_all_state()` — each module loads its cursor / dedup / hysteresis from `GET /component-state/{name}`.
2. Each tick: any module whose cycle has elapsed runs `process_cycle()`. Output is submitted to core as verdicts (CloudEvents-wrapped) or assessments.
3. After each cycle: `put_component_state(name, get_state())` — state survives restart.
4. Heartbeat emitted to core only when ≥1 module ran on this tick (avoids tight-loop heartbeat noise).
5. SIGTERM/SIGINT → graceful shutdown → final state persist + heartbeat.

Cycle failures are logged and counted, never raised. The runner keeps going.

## Configuration

Worker-specific config lives under `workers.{module}.*` in the unified `nthlayer.yaml` consumed via [`nthlayer_common.config.Config`](https://github.com/rsionnach/nthlayer-common). Examples:

```yaml
workers:
  respond:
    cycle_interval_seconds: 30.0
    fallback_threshold_seconds: 60.0
    terminal_retention_seconds: 86400.0
    step_timeout_seconds: 90.0
  learn:
    expiry_threshold_days: 7
```

LLM models are resolved via `NTHLAYER_MODEL` (default `anthropic/claude-sonnet-4-20250514`).

## NthLayer ecosystem

| Repo | Tier | Role |
|---|---|---|
| [`opensrm`](https://github.com/rsionnach/opensrm) | — | The OpenSRM specification |
| [`nthlayer-common`](https://github.com/rsionnach/nthlayer-common) | — | Shared library — verdicts, manifests, LLM wrapper, CoreAPIClient |
| [`nthlayer-generate`](https://github.com/rsionnach/nthlayer-generate) | — | Build-time compiler |
| [`nthlayer-core`](https://github.com/rsionnach/nthlayer-core) | **1** | HTTP API + state |
| [`nthlayer-workers`](https://github.com/rsionnach/nthlayer-workers) | **2** | This repo |
| [`nthlayer-bench`](https://github.com/rsionnach/nthlayer-bench) | **3** | Operator TUI |
| [`nthlayer`](https://github.com/rsionnach/nthlayer) | — | Project front door + meta-package |

The v1.5 consolidation absorbed five previously-standalone repos (`nthlayer-observe`, `nthlayer-measure`, `nthlayer-correlate`, `nthlayer-respond`, `nthlayer-learn`) into this single process. Those repos are now archived with deprecation releases on PyPI pointing here.

## Licence

Apache 2.0
