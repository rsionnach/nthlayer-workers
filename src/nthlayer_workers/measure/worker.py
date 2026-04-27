"""Measure worker module — three-type output from judgment SLO evaluation.

Single MeasureModule producing three output types per process_cycle():
1. judgment_slo_evaluation assessments (continuous, every cycle)
2. quality_breach verdicts (on HEALTHY→BREACH transition only)
3. autonomy_change verdicts (on governance reduction, once per breach)

State tracks per-SLO status, per-breach autonomy decisions, per-agent
autonomy levels, and a cursor for time-based polling.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from nthlayer_common.api_client import CoreAPIClient
from nthlayer_common.providers import PrometheusProvider

logger = structlog.get_logger()


@dataclass
class MeasureModule:
    """Judgment SLO evaluation with breach detection and autonomy governance.

    Three phases per cycle:
    1. Evaluate judgment SLOs → judgment_slo_evaluation assessments
    2. Detect breach transitions → quality_breach verdicts
    3. Governance on new breaches → autonomy_change verdicts (once per breach)

    Note: _evaluate_governance is a v1.5 stub (always returns "reduced").
    P3-C.3 replaces it with an Instructor-backed LLM evaluation.
    """

    client: CoreAPIClient
    prometheus_url: str
    eval_concurrency: int = 5

    def __post_init__(self):
        self._eval_concurrency = self.eval_concurrency
        self._slo_status: dict[str, str] = {}          # slo_key → "healthy"|"breach"
        self._breach_decisions: dict[str, dict] = {}    # slo_key → {decided, decided_at, ...}
        self._autonomy_levels: dict[str, str] = {}      # service → level
        self._breach_severities: dict[str, str] = {}    # slo_key → "low"|"high"|"critical"

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
                    "created_at": datetime.now(timezone.utc).isoformat(),
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
                await self.client.submit_verdict(breach_verdict)

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

                now = datetime.now(timezone.utc)
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
                            "reason": f"Autonomy reduced: {len(breaching_keys)} SLO(s) breaching",
                        },
                    }
                    await self.client.submit_verdict(verdict)

            # Clear breach state for recovered SLOs.
            # Intentionally does NOT restore _autonomy_levels — ratchet is one-way.
            for slo_key in list(self._breach_decisions):
                if current_status.get(slo_key) == "healthy":
                    del self._breach_decisions[slo_key]
                    self._breach_severities.pop(slo_key, None)

            # Carry forward status for SLOs that failed to evaluate this cycle
            # (Prometheus transient failure). Prevents false transitions on recovery.
            for key, status in self._slo_status.items():
                if key not in current_status:
                    current_status[key] = status

            # Update state
            self._slo_status = current_status

        finally:
            await provider.aclose()

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

        # Determine breach: for judgment SLOs, SLI >= target means healthy
        # (inverted from classical: reversal_rate SLI is 1 - reversal_rate,
        # so SLI >= target means rate is below threshold)
        status = "healthy" if current_value >= target else "breach"
        current_status[slo_key] = status

        now = datetime.now(timezone.utc)
        assessment_id = f"jse-{service}-{slo_name}-{uuid.uuid4().hex[:8]}"
        evaluation_ids[slo_key] = assessment_id
        evaluation_ids[f"__val__{slo_key}"] = current_value  # for severity classification

        assessment = {
            "id": assessment_id,
            "created_at": now.isoformat(),
            "kind": "judgment_slo_evaluation",
            "service": service,
            "data": {
                "slo_name": slo_name,
                "slo_type": slo.get("judgment_type"),
                "target": target,
                "current_value": round(current_value, 6),
                "status": status,
                "window": slo.get("window", "30d"),
            },
        }
        result = await self.client.submit_assessment(assessment)
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
    if judgment_type == "stability":
        return "high"  # any stability breach is high; sustained tracking is v2
    if judgment_type == "segments":
        return _classify_variance(target, current_value)
    if judgment_type == "calibration":
        return _classify_calibration(target, current_value)
    # Unknown type → "high" for safety
    return "high"


def _classify_budget_consumption(target: float, current_value: float) -> str:
    """Classify by error budget consumption percentage.

    budget_consumption = (target - value) / (1 - target) × 100
    <200% = low, 200-500% = high, >500% = critical
    """
    budget = 1.0 - target
    if budget <= 0:
        return "critical"  # target=1.0 means zero budget, any miss is critical
    consumption = (target - current_value) / budget * 100
    if consumption < 200:
        return "low"
    if consumption < 500:
        return "high"
    return "critical"


def _classify_variance(target: float, current_value: float) -> str:
    """Classify segments breach by variance ratio.

    Precondition: current_value < target (caller verified breach).
    <2x = low, 2-5x = high, >5x = critical
    """
    if target <= 0:
        return "high"
    ratio = (target - current_value) / target  # positive when breaching
    if ratio < 2:
        return "low"
    if ratio < 5:
        return "high"
    return "critical"


def _classify_calibration(target: float, current_value: float) -> str:
    """Classify calibration breach by delta.

    <0.1 = low, 0.1-0.3 = high, >0.3 = critical
    """
    delta = abs(current_value - target)
    if delta < 0.1:
        return "low"
    if delta < 0.3:
        return "high"
    return "critical"
