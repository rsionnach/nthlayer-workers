"""Measure worker module — judgment SLO and judgment threshold evaluation.

Single MeasureModule; output types per process_cycle():
1. judgment_slo_evaluation assessments (continuous, every cycle)
2. judgment_threshold_evaluation assessments (threshold phase, on its own
   MEASURE_THRESHOLD_INTERVAL cadence, opensrm-g32d)
3. quality_breach verdicts (SLO HEALTHY→BREACH transition; threshold breach
   on firing, always severity "high")
4. autonomy_change verdicts (on governance reduction, once per breach)

Threshold breaches reach governance only with MEASURE_THRESHOLD_AUTONOMY on.

State tracks per-SLO status, per-breach autonomy decisions, per-agent
autonomy levels, per-threshold-key hysteresis state, and the threshold
phase's last run time.
"""

from __future__ import annotations

import asyncio
import math
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog
from nthlayer_common.api_client import CoreAPIClient
from nthlayer_common.cloudevents import wrap_assessment, wrap_verdict
from nthlayer_common.providers import PrometheusProvider

from nthlayer_workers.measure.thresholds.phase import (
    DEFAULT_THRESHOLD_HEARTBEAT_SECONDS,
    DEFAULT_THRESHOLD_INTERVAL_SECONDS,
    THRESHOLD_BREACH_SEVERITY,
    THRESHOLD_PHASE_TIMEOUT_SECONDS,
    retire_thresholds,
    run_threshold_phase,
)
from nthlayer_workers.measure.thresholds.state import REARM_INTERVALS, KeyState, as_timestamp

logger = structlog.get_logger()


def _env_flag_default_on(name: str) -> bool:
    """A default-on operator switch: only off / false / 0 (any case) disable it."""
    return os.environ.get(name, "").strip().lower() not in ("off", "false", "0")


def _env_flag(name: str) -> bool:
    """A default-off operator switch: 1 / true / yes / on (any case) enable it."""
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


# Upper bound for the threshold timing settings (7 days). Zero is rejected:
# a zero interval would evaluate every cycle and collapse re-arm hysteresis.
_MAX_TIMING_SECONDS = 7 * 86400.0

# Layout of the persisted threshold_state / threshold_governed. Bump it when
# that layout (or threshold_key) changes incompatibly: restore_state discards
# threshold state of any version it does not know.
THRESHOLD_STATE_VERSION = 1


def _env_seconds(name: str, default: float) -> float:
    """Seconds from the environment, in (0, 7d]. Unset: the default. Not a
    number, non-finite, zero, negative or over 7d: the default, with a warning."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not (math.isfinite(value) and 0 < value <= _MAX_TIMING_SECONDS):
        logger.warning("measure_env_invalid", name=name, value=raw, default=default)
        return default
    return value


@dataclass
class MeasureModule:
    """Judgment SLO evaluation with breach detection and autonomy governance.

    Per cycle:
    1. Evaluate judgment SLOs → judgment_slo_evaluation assessments
    1b. Threshold phase, only when threshold_interval_seconds have passed
        (otherwise just retire removed entries) → judgment_threshold_evaluation
        assessments and, on a firing breach, quality_breach verdicts (high)
    2. Detect SLO breach transitions → quality_breach verdicts
    2b. With threshold_autonomy (MEASURE_THRESHOLD_AUTONOMY, default off):
        inject new threshold breaches (as "high") and re-inject open,
        already-governed ones so they count like open SLO breaches
    3. Governance on new breaches → autonomy_change verdicts (once per breach);
       then release threshold keys that re-armed or retired

    Note: _evaluate_governance is a v1.5 stub (always returns "reduced").
    P3-C.3 replaces it with an Instructor-backed LLM evaluation.
    """

    client: CoreAPIClient
    prometheus_url: str
    eval_concurrency: int = 5
    # Operator settings, read once at construction: changing one needs a restart.
    retention_check: bool = field(
        default_factory=lambda: _env_flag_default_on("MEASURE_RETENTION_CHECK")
    )
    threshold_interval_seconds: float = field(
        default_factory=lambda: _env_seconds(
            "MEASURE_THRESHOLD_INTERVAL", DEFAULT_THRESHOLD_INTERVAL_SECONDS
        )
    )
    threshold_heartbeat_seconds: float = field(
        default_factory=lambda: _env_seconds(
            "MEASURE_THRESHOLD_HEARTBEAT", DEFAULT_THRESHOLD_HEARTBEAT_SECONDS
        )
    )
    # Default off: when on, judgment-threshold breaches feed the autonomy
    # ratchet (Phase 3) as "high" breaches.
    threshold_autonomy: bool = field(
        default_factory=lambda: _env_flag("MEASURE_THRESHOLD_AUTONOMY")
    )
    # Wall-clock seconds; injectable so tests control the threshold cadence.
    clock: Callable[[], float] = time.time

    def __post_init__(self):
        self._eval_concurrency = self.eval_concurrency
        self._slo_status: dict[str, str] = {}          # slo_key → "healthy"|"breach"
        self._breach_decisions: dict[str, dict] = {}    # slo_key → {decided, decided_at, ...}
        self._autonomy_levels: dict[str, str] = {}      # service → level
        self._breach_severities: dict[str, str] = {}    # slo_key → "low"|"high"|"critical"
        # Judgment thresholds (opensrm-g32d): per-key state, persisted.
        self._threshold_state: dict[str, dict] = {}
        # Once-per-change log memory; in-memory only, so a restart logs each
        # problem once more.
        self._threshold_problems: set[str] = set()
        self._threshold_last_run: float | None = None
        # Governance keys (_breach_decisions / _breach_severities) that are
        # threshold keys, so threshold cleanup never touches SLO keys.
        self._threshold_governed: set[str] = set()
        # Rotates where each threshold run starts (tail starvation under the
        # time budget). In-memory only: after a restart it starts at 0.
        self._threshold_rotation = 0

    @property
    def name(self) -> str:
        return "measure"

    async def restore_state(self, state: dict | None) -> None:
        if not state:
            return
        self._slo_status = state.get("slo_status", {})
        self._breach_decisions = state.get("breach_decisions", {})
        self._autonomy_levels = state.get("autonomy_levels", {})
        self._breach_severities = state.get("breach_severities", {})
        self._threshold_last_run = as_timestamp(state.get("threshold_last_run"))
        # Absent: written before the marker existed, which is version 1. Any
        # other version is a layout this release cannot read: start threshold
        # state afresh (an open breach may re-raise, the safe direction).
        version = state.get("threshold_state_version", THRESHOLD_STATE_VERSION)
        if type(version) is not int or version != THRESHOLD_STATE_VERSION:
            logger.warning("threshold_state_version_unknown", version=version)
            self._threshold_state, self._threshold_governed = {}, set()
            return
        # Restored component_state is untrusted; KeyState.from_dict handles
        # wrong-typed fields, so only the entry shape is checked here.
        raw = state.get("threshold_state")
        self._threshold_state = (
            {k: v for k, v in raw.items()
             if isinstance(k, str) and isinstance(v, dict) and isinstance(v.get("service"), str)}
            if isinstance(raw, dict) else {}
        )
        # Only keys with restored threshold state: an SLO key smuggled in here
        # must never let threshold cleanup wipe that SLO's decision.
        raw_governed = state.get("threshold_governed")
        self._threshold_governed = (
            {k for k in raw_governed if isinstance(k, str) and k in self._threshold_state}
            if isinstance(raw_governed, list) else set()
        )

    async def process_cycle(self) -> None:
        # Fetch manifests with judgment SLOs
        result = await self.client.get_manifests()
        if not result.ok or not result.data:
            return

        manifests = result.data
        provider = PrometheusProvider(self.prometheus_url)

        try:
            current_status: dict[str, str] = {}
            evaluation_ids: dict[str, str] = {}  # slo_key → assessment_id

            # Phase 1: Evaluate each judgment SLO (parallel with concurrency cap)
            slos_to_eval = []
            for m in manifests:
                service = m.get("name")
                if not service:
                    continue
                for slo in m.get("slos", []):
                    if not slo.get("judgment_type"):
                        continue
                    slos_to_eval.append((service, slo))

            sem = asyncio.Semaphore(self._eval_concurrency)

            async def eval_one(svc: str, slo_data: dict) -> None:
                async with sem:
                    try:
                        await self._evaluate_slo(
                            svc, slo_data, provider, current_status, evaluation_ids
                        )
                    except Exception:
                        logger.warning("measure_eval_failed", service=svc,
                                       slo=slo_data.get("name"))

            await asyncio.gather(
                *(eval_one(svc, slo) for svc, slo in slos_to_eval),
                return_exceptions=True,
            )

            # Phase 1b: threshold phase (own cadence).
            threshold_breaches = await self._maybe_run_threshold_phase(manifests, provider)
            if threshold_breaches:
                logger.info("threshold_breaches", count=len(threshold_breaches),
                            keys=[key for key, _, _ in threshold_breaches],
                            services=sorted({svc for _, svc, _ in threshold_breaches}))

            # Phase 2: Detect breach transitions + classify severity
            new_breaches = _detect_transitions(current_status, self._slo_status)
            breach_verdict_ids: dict[str, str] = {}
            for slo_key in new_breaches:
                parts = slo_key.split(":", 1)
                service = parts[0] if parts else "unknown"
                slo_name = parts[1] if len(parts) > 1 else "unknown"

                # Find SLO data for severity classification
                slo_data = self._find_slo(manifests, service, slo_name)
                target = slo_data.get("target", 0) if slo_data else 0
                judgment_type = slo_data.get("judgment_type") if slo_data else None
                current_value = evaluation_ids.get(f"__val__{slo_key}", 0)

                severity = classify_severity(judgment_type, target, current_value)
                self._breach_severities[slo_key] = severity

                breach_id = f"vrd-breach-{service}-{slo_name}-{uuid.uuid4().hex[:8]}"
                breach_verdict_ids[slo_key] = breach_id
                breach_verdict = {
                    "id": breach_id,
                    "created_at": datetime.now(UTC).isoformat(),
                    "type": "quality_breach",
                    "service": service,
                    "parent_ids": [evaluation_ids.get(slo_key, "")],
                    "data": {
                        "slo_name": slo_name,
                        "slo_key": slo_key,
                        "severity": severity,
                        "judgment_type": judgment_type,
                        "target": target,
                        "current_value": current_value,
                    },
                }
                await self.client.submit_verdict(
                    wrap_verdict(breach_verdict, component="measure")
                )

            # Phase 2b: threshold breaches into governance (toggle-gated).
            governed_threshold_keys = self._inject_threshold_breaches(
                threshold_breaches, current_status, new_breaches, breach_verdict_ids
            )

            # Phase 3: Governance — severity-based, fully deterministic (no LLM in v1.5)
            # Process per-service: count breaching SLOs, determine action
            breaching_services: dict[str, list[str]] = {}
            for key, status in current_status.items():
                if status == "breach":
                    svc = key.split(":", 1)[0]
                    breaching_services.setdefault(svc, []).append(key)

            for service, breaching_keys in breaching_services.items():
                # Only act on services with new breaches this cycle
                if not any(k in new_breaches for k in breaching_keys):
                    continue
                # Skip if all breaches already have governance decisions
                undecided = [
                    k for k in breaching_keys
                    if not self._breach_decisions.get(k, {}).get("decided")
                ]
                if not undecided:
                    continue

                action, steps = self._compute_governance(service, breaching_keys)

                now = datetime.now(UTC)
                for k in undecided:
                    self._breach_decisions[k] = {
                        "decided": True,
                        "decided_at": now.isoformat(),
                        "autonomy_action": action,
                    }

                if action == "reduced":
                    current_level = self._autonomy_levels.get(service, "fully_autonomous")
                    new_level = _reduce_autonomy(current_level, steps)
                    self._autonomy_levels[service] = new_level

                    verdict = {
                        "id": f"vrd-auto-{service}-{uuid.uuid4().hex[:8]}",
                        "created_at": now.isoformat(),
                        "type": "autonomy_change",
                        "service": service,
                        "parent_ids": [breach_verdict_ids.get(k, "") for k in undecided if k in breach_verdict_ids],
                        "data": {
                            "previous_level": current_level,
                            "new_level": new_level,
                            "breaching_slo_count": len(breaching_keys),
                            "reason": (
                                f"Autonomy reduced: {len(breaching_keys)} "
                                "SLO/threshold breach(es)"
                            ),
                        },
                    }
                    await self.client.submit_verdict(
                        wrap_verdict(verdict, component="measure")
                    )

            # Clear breach state for recovered SLOs.
            # Intentionally does NOT restore _autonomy_levels — ratchet is one-way.
            for slo_key in list(self._breach_decisions):
                if current_status.get(slo_key) == "healthy":
                    del self._breach_decisions[slo_key]
                    self._breach_severities.pop(slo_key, None)

            self._release_threshold_governance()

            # Threshold keys are re-injected from _threshold_state each cycle;
            # keep _slo_status SLO-only.
            for key in governed_threshold_keys:
                current_status.pop(key, None)

            # Carry forward status for SLOs that failed to evaluate this cycle
            # (Prometheus transient failure). Prevents false transitions on recovery.
            for key, status in self._slo_status.items():
                if key not in current_status:
                    current_status[key] = status

            # Update state
            self._slo_status = current_status

        finally:
            await provider.aclose()

    async def _maybe_run_threshold_phase(
        self, manifests: list[dict], provider: PrometheusProvider
    ) -> list[tuple[str, str, str]]:
        """Threshold phase (opensrm-g32d), on its own cadence: it runs when
        threshold_interval_seconds have passed since it last ran (or it never
        has, or the clock went back); otherwise only removed entries retire.
        Returns this run's new breaches as (key, service, breach_verdict_id).
        Open threshold breaches still reach governance every cycle, from
        _threshold_state. It must never stop Phases 2-3: on failure, log and
        go on; a run is bounded by THRESHOLD_PHASE_TIMEOUT_SECONDS, and on a
        timeout the breaches core already accepted (their state has
        advanced) still go to governance. State carried across a sibling
        re-key renames its governance entries too, on run and not-run
        cycles alike."""
        threshold_breaches: list[tuple[str, str, str]] = []
        renamed: dict[str, str] = {}
        unreached: set[str] = set()
        now = self.clock()
        last = self._threshold_last_run
        if last is None or now < last or now - last >= self.threshold_interval_seconds:
            # Advanced before the run, so even a phase that raises outright
            # retries after one interval, not every cycle (no log flood).
            self._threshold_last_run = now
            rotation = self._threshold_rotation
            self._threshold_rotation += 1
            try:
                await asyncio.wait_for(
                    run_threshold_phase(
                        client=self.client, provider=provider, manifests=manifests,
                        state=self._threshold_state, seen_problems=self._threshold_problems,
                        eval_concurrency=self._eval_concurrency,
                        retention_check=self.retention_check, now=now,
                        rearm_seconds=REARM_INTERVALS * self.threshold_interval_seconds,
                        heartbeat_seconds=self.threshold_heartbeat_seconds,
                        renamed=renamed, breaches_out=threshold_breaches,
                        unreached=unreached, rotation=rotation,
                    ),
                    THRESHOLD_PHASE_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                logger.warning("threshold_phase_timeout", timeout=THRESHOLD_PHASE_TIMEOUT_SECONDS,
                               accepted_breaches=len(threshold_breaches),
                               unreached=len(unreached))
            except Exception as exc:
                logger.warning("threshold_phase_failed", error=f"{type(exc).__name__}: {exc}")
        else:
            # Not a threshold run: still retire removed entries, since
            # /manifests is fresh every cycle.
            try:
                retire_thresholds(manifests=manifests, state=self._threshold_state,
                                  seen_problems=self._threshold_problems, renamed=renamed)
            except Exception as exc:
                logger.warning("threshold_retire_failed", error=f"{type(exc).__name__}: {exc}")
        # Before Phase 3 and _release_threshold_governance, which would
        # otherwise see the old key as retired and drop its decision.
        self._rename_threshold_governance(renamed)
        return list(threshold_breaches)

    def _rename_threshold_governance(self, renamed: dict[str, str]) -> None:
        """Move governance entries of re-keyed threshold state to the new key,
        so an open breach keeps its decision (no second autonomy step).
        Only keys in _threshold_governed: _breach_decisions also holds SLO
        keys."""
        for old, new in renamed.items():
            if old not in self._threshold_governed:
                continue
            for store in (self._breach_decisions, self._breach_severities):
                if old in store:
                    store[new] = store.pop(old)
            self._threshold_governed.discard(old)
            self._threshold_governed.add(new)

    def _inject_threshold_breaches(
        self,
        threshold_breaches: list[tuple[str, str, str]],
        current_status: dict[str, str],
        new_breaches: list[str],
        breach_verdict_ids: dict[str, str],
    ) -> list[str]:
        """Phase 2b: with threshold_autonomy on, put threshold breaches into
        Phase 3's inputs (mutating the passed locals). Returns the threshold
        keys injected, which the caller pops from current_status before it
        becomes _slo_status."""
        governed_threshold_keys: list[str] = []
        # Turning the toggle off leaves governed keys' decisions in place (Phase 3
        # never reads them while off). Turning it back on re-injects breaches that
        # were governed before it went off; a breach that opened while it was off
        # does not count (it never had a governance decision).
        if self.threshold_autonomy:
            # Open threshold breaches count toward governance every cycle,
            # exactly as an open SLO breach stays in current_status - so a
            # later SLO breach sees "multiple breaches" (spec: same rules).
            # Open means disarmed: hysteresis, not one within evaluation,
            # defines recovery, so a flapping breach keeps counting.
            for key, st in self._threshold_state.items():
                if not KeyState.from_dict(st).armed and self._breach_decisions.get(key):
                    current_status[key] = "breach"
                    governed_threshold_keys.append(key)
            # New threshold breaches this cycle enter as new high breaches.
            for key, _svc, breach_id in threshold_breaches:
                current_status[key] = "breach"
                new_breaches.append(key)
                breach_verdict_ids[key] = breach_id
                self._breach_severities[key] = THRESHOLD_BREACH_SEVERITY
                governed_threshold_keys.append(key)
        self._threshold_governed.update(governed_threshold_keys)
        return governed_threshold_keys

    def _release_threshold_governance(self) -> None:
        """Threshold keys leave governance when retired or re-armed (the
        hysteresis's recovery). Iterates _threshold_governed only:
        _breach_decisions also holds SLO keys. The ratchet stays one-way."""
        for key in list(self._threshold_governed):
            st = self._threshold_state.get(key)
            if st is None or KeyState.from_dict(st).armed:   # retired or re-armed
                self._breach_decisions.pop(key, None)
                self._breach_severities.pop(key, None)
                self._threshold_governed.discard(key)

    async def _evaluate_slo(
        self,
        service: str,
        slo: dict,
        provider: PrometheusProvider,
        current_status: dict[str, str],
        evaluation_ids: dict[str, str],
    ) -> None:
        """Evaluate a single judgment SLO and submit assessment."""
        slo_name = slo.get("name", "unknown")
        slo_key = f"{service}:{slo_name}"
        target = slo.get("target", 0)
        indicator_query = slo.get("indicator_query")

        if not indicator_query:
            return

        try:
            current_value = await provider.get_sli_value(indicator_query)
        except Exception:
            logger.debug("measure_prometheus_failed", service=service, slo=slo_name)
            return

        if current_value is None:
            logger.debug("measure_no_data", service=service, slo=slo_name)
            return

        # Scale Prometheus SLI (always 0.0-1.0 ratio) to the canonical
        # 0-100 percentage convention used for SLO targets (opensrm-5fff.1).
        current_pct = current_value * 100

        # Determine breach: for judgment SLOs, SLI >= target means healthy
        # (inverted from classical: reversal_rate SLI is 1 - reversal_rate,
        # so SLI >= target means rate is below threshold).
        status = "healthy" if current_pct >= target else "breach"
        current_status[slo_key] = status

        now = datetime.now(UTC)
        assessment_id = f"jse-{service}-{slo_name}-{uuid.uuid4().hex[:8]}"
        evaluation_ids[slo_key] = assessment_id
        evaluation_ids[f"__val__{slo_key}"] = current_pct  # for severity classification (percentage)

        assessment = {
            "id": assessment_id,
            "created_at": now.isoformat(),
            "kind": "judgment_slo_evaluation",
            "service": service,
            "data": {
                "slo_name": slo_name,
                "slo_type": slo.get("judgment_type"),
                "target": target,
                "current_value": round(current_pct, 6),
                "status": status,
                "window": slo.get("window", "30d"),
            },
        }
        result = await self.client.submit_assessment(
            wrap_assessment(assessment, component="measure")
        )
        if not result.ok:
            logger.warning("measure_assessment_submit_failed",
                           service=service, slo=slo_name)

    def _compute_governance(self, service: str, breaching_keys: list[str]) -> tuple[str, int]:
        """Compute governance action based on breach count and severity.

        Fully deterministic in v1.5 — no LLM calls. v2 may add LLM reasoning
        for borderline cases. v1.5's conservative rules may produce unnecessary
        reductions; manual elevation handles those.

        Returns (action, steps):
        Positive steps = relative drop (move N positions toward observer).
        Negative steps = absolute floor (-1 = observer, -2 = advisor).
        """
        if not breaching_keys:
            return "no_change", 0

        if len(breaching_keys) > 1:
            # Multiple simultaneous breaches
            any_critical = any(
                self._breach_severities.get(k) == "critical"
                for k in breaching_keys
            )
            if any_critical:
                return "reduced", -1  # floor=observer (idx 4, most restrictive)
            return "reduced", -2  # floor=advisor (idx 3, less restrictive than observer)

        # Single breach — severity determines drop
        severity = self._breach_severities.get(breaching_keys[0], "high")
        if severity == "low":
            return "reduced", 1
        if severity == "high":
            return "reduced", 2
        if severity == "critical":
            return "reduced", -1  # drop to observer
        return "reduced", 1  # fallback

    @staticmethod
    def _find_slo(manifests: list[dict], service: str, slo_name: str) -> dict | None:
        """Find SLO data from manifests for severity classification."""
        for m in manifests:
            if m.get("name") != service:
                continue
            for slo in m.get("slos", []):
                if slo.get("name") == slo_name:
                    return slo
        return None

    async def get_state(self) -> dict:
        return {
            "slo_status": self._slo_status,
            "breach_decisions": self._breach_decisions,
            "autonomy_levels": self._autonomy_levels,
            "breach_severities": self._breach_severities,
            "threshold_state": self._threshold_state,
            "threshold_last_run": self._threshold_last_run,
            "threshold_governed": sorted(self._threshold_governed),
            "threshold_state_version": THRESHOLD_STATE_VERSION,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _detect_transitions(
    current: dict[str, str],
    previous: dict[str, str],
) -> list[str]:
    """Return SLO keys that transitioned HEALTHY→BREACH."""
    transitions = []
    for key, status in current.items():
        prev = previous.get(key, "unknown")
        if status == "breach" and prev != "breach":
            transitions.append(key)
    return transitions


# Ordered least→most restrictive. "observer" is the floor — agent can only watch.
AUTONOMY_LADDER = [
    "fully_autonomous", "autonomous", "limited_autonomous", "advisor", "observer"
]


def _reduce_autonomy(current_level: str, steps: int = 1) -> str:
    """Reduce autonomy by steps. One-way ratchet — never promotes upward.

    Positive steps = relative drop (move N positions toward observer).
    Negative steps = absolute floor (-1 = observer, -2 = advisor),
    but clamped so we never promote above current level.
    """
    # Unknown level: fail safe to advisor (conservative low-trust default).
    # If v2 renames a level or a deployment uses custom names, unrecognised
    # agents default to low-trust rather than full autonomy.
    if current_level not in AUTONOMY_LADDER:
        return "advisor"

    current_idx = AUTONOMY_LADDER.index(current_level)

    if steps == -1:
        target_idx = AUTONOMY_LADDER.index("observer")
    elif steps == -2:
        target_idx = AUTONOMY_LADDER.index("advisor")
    else:
        target_idx = min(current_idx + steps, len(AUTONOMY_LADDER) - 1)

    # One-way ratchet: never promote (target must be >= current)
    final_idx = max(current_idx, target_idx)
    return AUTONOMY_LADDER[final_idx]


# Budget-consumption severity types (use error budget ratio)
_BUDGET_CONSUMPTION_TYPES = frozenset({
    "reversal_rate", "high_confidence_failure", "escalation",
    "outcomes", "audit_sampling",
})


def classify_severity(
    judgment_type: str | None,
    target: float,
    current_value: float,
) -> str:
    """Classify breach severity per judgment SLO type.

    Returns 'low', 'high', or 'critical'. Fully deterministic.
    """
    if judgment_type in _BUDGET_CONSUMPTION_TYPES:
        return _classify_budget_consumption(target, current_value)
    # Unknown type → "high" for safety
    return "high"


def _classify_budget_consumption(target: float, current_value: float) -> str:
    """Classify by error budget consumption percentage.

    Inputs are in 0-100 percentage convention (canonical, opensrm-5fff.1).
    For target=98.5 and current_value=92.0:
      error_budget_pct = 100 - 98.5 = 1.5 (percentage points of budget)
      breach_pct       = 98.5 - 92.0 = 6.5 (percentage points consumed)
      consumption      = 6.5 / 1.5 * 100 = 433%

    <200% = low, 200-500% = high, >500% = critical.
    """
    error_budget_pct = 100.0 - target
    if error_budget_pct <= 0:
        return "critical"  # target=100 means zero budget, any miss is critical
    consumption = (target - current_value) / error_budget_pct * 100
    if consumption < 200:
        return "low"
    if consumption < 500:
        return "high"
    return "critical"
