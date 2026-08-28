"""Learn worker modules — outcome resolution and retrospective generation.

Two modules implementing WorkerModule protocol:
- LearnOutcomeModule: continuous verdict outcome resolution + calibration signals (60s)
- LearnRetrospectiveModule: incident retrospective triggered by correlation_snapshot (30s)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from nthlayer_common.api_client import CoreAPIClient
from nthlayer_common.cloudevents import wrap_assessment
from nthlayer_common.manifest import extract_declared_dependencies

from nthlayer_workers.learn._trigger import resolve_trigger_service

logger = structlog.get_logger()

# Calibration delta: observed_outcome_score mapping
_OUTCOME_SCORES = {
    "confirmed": 1.0,
    "overridden": 0.0,
    "partial": 0.5,
}


# ---------------------------------------------------------------------------
# LearnOutcomeModule — continuous verdict outcome resolution
# ---------------------------------------------------------------------------


@dataclass
class LearnOutcomeModule:
    """Continuous background maintenance of verdict outcome state.

    Each process_cycle():
    1. Fetches pending verdicts older than minimum_resolution_age
    2. Attempts resolution via five paths (lineage, calibration sampling,
       downstream signal, score-outcome divergence, expiry)
    3. Emits calibration_signal assessment when outcomes resolve
    4. Marks expired verdicts past threshold

    Calibration signals feed measure module's self-calibration (v2).
    Expired verdicts don't produce signals — absence of data is not
    a quality signal.
    """

    client: CoreAPIClient
    expiry_threshold_days: int = 7
    minimum_resolution_age_hours: int = 1
    _cursor: str | None = None

    @property
    def name(self) -> str:
        return "learn.outcome"

    async def restore_state(self, state: dict | None) -> None:
        if state:
            self._cursor = state.get("cursor")

    async def process_cycle(self) -> None:
        now = datetime.now(UTC)

        # Minimum age floor: skip verdicts younger than threshold because
        # downstream signals may not have arrived yet. Premature resolution
        # attempts always return "no signal found" and waste cycles.
        age_cutoff = now - timedelta(hours=self.minimum_resolution_age_hours)

        result = await self.client.get_verdicts(
            created_before=age_cutoff.isoformat(),
            created_after=self._cursor,
            limit=50,
        )
        if not result.ok or not result.data:
            return

        for verdict in result.data:
            outcome = verdict.get("outcome", {})
            if outcome.get("status") != "pending":
                continue

            try:
                # Attempt resolution via five paths
                resolution = await self._attempt_resolution(verdict)
                if resolution:
                    submit = await self.client.resolve_outcome(verdict["id"], resolution)
                    if submit.ok:
                        await self._emit_calibration_signal(verdict, resolution)
                    continue

                # Expiry fallback
                if self._is_past_expiry(verdict, now):
                    await self.client.resolve_outcome(verdict["id"], {
                        "outcome_status": "expired",
                        "resolution": "No outcome signal within threshold",
                    })
            except Exception:
                logger.warning("outcome_resolution_failed", verdict_id=verdict.get("id"))

            # Advance cursor past this verdict regardless of outcome
            created = verdict.get("created_at", "")
            if created and (not self._cursor or created > self._cursor):
                self._cursor = created

    async def _attempt_resolution(self, verdict: dict) -> dict | None:
        """Try the five resolution paths in order. Returns resolution dict or None."""
        verdict_id = verdict.get("id", "")

        # Paths 1+4: Lineage + divergence — single pass over descendants.
        # Check for downstream verdicts that resolve this one.
        # Priority: overridden > confirmed (an execution that was overridden
        # is a divergence signal, not a confirmation).
        descendants = await self.client.get_descendants(verdict_id)
        if descendants.ok and descendants.data:
            has_execution = None
            has_overridden = None
            for desc in descendants.data:
                desc_type = desc.get("type", "")
                desc_outcome = desc.get("outcome", {}).get("status")
                if desc_outcome == "overridden" and has_overridden is None:
                    has_overridden = desc
                if desc_type in ("execution", "outcome_resolution") and has_execution is None:
                    has_execution = desc

            # Overridden takes priority (Path 4: divergence)
            if has_overridden:
                return {
                    "outcome_status": "overridden",
                    "resolution": f"Overridden by {has_overridden.get('id')}",
                    "path": "divergence",
                }
            # Execution/outcome_resolution confirms (Path 1: lineage)
            if has_execution:
                return {
                    "outcome_status": "confirmed",
                    "resolution": f"Resolved by downstream {has_execution.get('type')} verdict {has_execution.get('id')}",
                    "path": "lineage",
                }

        # Path 2: Calibration sampling — v1.5 stub (infrastructure not yet built)
        # Path 3: Downstream signal — captured by lineage when external signals
        #   create verdicts with parent_ids pointing to this verdict.
        # Path 5: Expiry — handled in the caller's expiry fallback
        return None

    def _is_past_expiry(self, verdict: dict, now: datetime) -> bool:
        """Check if a verdict has exceeded the expiry threshold."""
        if self.expiry_threshold_days <= 0:
            return False  # 0 or negative threshold disables expiry
        created = verdict.get("created_at", "")
        if not created:
            return False
        try:
            created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            return (now - created_dt) >= timedelta(days=self.expiry_threshold_days)
        except (ValueError, AttributeError):
            return False

    async def _emit_calibration_signal(self, verdict: dict, resolution: dict) -> None:
        """Emit calibration_signal assessment for a resolved verdict."""
        judgment = verdict.get("judgment", {})
        confidence = judgment.get("confidence")
        outcome_status = resolution.get("outcome_status", "")

        # Compute calibration delta
        delta = None
        if confidence is not None and outcome_status in _OUTCOME_SCORES:
            delta = abs(confidence - _OUTCOME_SCORES[outcome_status])

        now = datetime.now(UTC)
        assessment = {
            "id": f"cal-{verdict.get('id', 'unknown')}-{uuid.uuid4().hex[:8]}",
            "created_at": now.isoformat(),
            "kind": "calibration_signal",
            "service": verdict.get("service", "unknown"),
            "data": {
                "verdict_id": verdict.get("id"),
                "verdict_type": verdict.get("type"),
                "expressed_confidence": confidence,
                "observed_outcome": outcome_status,
                "calibration_delta": round(delta, 4) if delta is not None else None,
                "resolution_path": resolution.get("path", "unknown"),
                "producer_system": verdict.get("producer", {}).get("system"),
            },
        }
        result = await self.client.submit_assessment(wrap_assessment(assessment, component="learn"))
        if not result.ok:
            logger.warning("calibration_signal_submit_failed", verdict_id=verdict.get("id"))

    async def get_state(self) -> dict:
        state: dict[str, Any] = {}
        if self._cursor:
            state["cursor"] = self._cursor
        return state


# ---------------------------------------------------------------------------
# LearnRetrospectiveModule — incident retrospective from correlation snapshots
# ---------------------------------------------------------------------------


@dataclass
class LearnRetrospectiveModule:
    """Generate retrospective assessments when correlation snapshots close incidents.

    Retrospectives are narrative: "What happened during this incident?"
    They describe decisions and events, not whether decisions were correct.
    Most verdicts in a fresh chain won't have outcomes resolved yet —
    that's fine. Outcome resolution is the learn module's separate
    calibration output (LearnOutcomeModule).
    """

    client: CoreAPIClient
    _cursor: str | None = None

    @property
    def name(self) -> str:
        return "learn.retrospective"

    async def restore_state(self, state: dict | None) -> None:
        if state:
            self._cursor = state.get("cursor")

    async def process_cycle(self) -> None:
        # Poll for new correlation_snapshot assessments
        result = await self.client.get_assessments(kind="correlation_snapshot")
        if not result.ok or not result.data:
            return

        for snapshot in result.data:
            created = snapshot.get("created_at", "")
            if self._cursor and created <= self._cursor:
                continue

            success = await self._generate_retrospective(snapshot)
            # Only advance cursor on successful submission
            if success and (not self._cursor or created > self._cursor):
                self._cursor = created

    async def _generate_retrospective(self, snapshot: dict) -> bool:
        """Generate and submit a retrospective assessment. Returns True on success."""
        snapshot_data = snapshot.get("data", {})
        domain = snapshot_data.get("domain", {})
        service = snapshot.get("service", domain.get("service", "unknown"))

        # opensrm-dpws: resolve trigger_service via correlation-first /
        # snapshot-service-fallback precedence. The snapshot's
        # data.domain.service IS the correlator's grouping anchor; the
        # top-level service field is the same value emitted at submit
        # time but kept independent for resilience.
        trigger_service = resolve_trigger_service(
            [domain.get("service")],
            snapshot.get("service"),
        )

        # Build verdict chain by querying verdicts for the affected service
        # during the snapshot window. Cannot use get_ancestors on an assessment
        # ID — that endpoint operates on verdict IDs only.
        chain = []
        window = snapshot_data.get("window", {})
        opened_at = window.get("opened_at")
        closed_at = window.get("closed_at")
        if opened_at and closed_at:
            chain_result = await self.client.get_verdicts(
                service=service,
                created_after=opened_at,
                created_before=closed_at,
                limit=100,
            )
            if chain_result.ok and chain_result.data:
                chain = chain_result.data

        # Build timeline
        timeline = _build_chain_timeline(chain)

        # Compute metrics
        resolved_count = sum(
            1 for v in chain
            if v.get("outcome", {}).get("status") not in (None, "pending")
        )
        pending_count = len(chain) - resolved_count

        # Extract root cause from correlation data
        root_cause = snapshot_data.get("correlation_groups", [{}])[0] if snapshot_data.get("correlation_groups") else None

        # opensrm-dpws: declared_dependencies_by_service — populate only
        # when the trigger's own manifest is in the API result.
        # _add_dependency_recommendations reads declared_map.get(trigger)
        # so non-trigger gaps are harmless; trigger gap → over-broad recs.
        declared_dependencies_by_service: dict[str, list[str]] | None = None
        if trigger_service:
            manifests_result = await self.client.get_manifests()
            if not manifests_result.ok:
                logger.warning(
                    "learn_manifest_fetch_failed",
                    error=manifests_result.error,
                )
            elif not manifests_result.data:
                logger.info("learn_manifest_catalogue_empty")
            else:
                manifest_names = {
                    m.get("name") for m in manifests_result.data if m.get("name")
                }
                if trigger_service not in manifest_names:
                    logger.warning(
                        "learn_trigger_manifest_absent",
                        service=trigger_service,
                    )
                else:
                    # Trigger-narrow per design § 3.4: downstream
                    # _add_dependency_recommendations reads
                    # declared_map.get(trigger) and never iterates other
                    # entries, so emit a 1-key dict rather than the full
                    # catalogue (smaller wire payload, intentional shape).
                    trigger_matches = [
                        m for m in manifests_result.data
                        if m.get("name") == trigger_service
                    ]
                    if len(trigger_matches) > 1:
                        # First-wins (matches the existing
                        # _load_manifests_from_specs dedup behaviour on the
                        # CLI side, which logs manifest_duplicate_skipped).
                        logger.warning(
                            "learn_manifest_duplicate_skipped",
                            service=trigger_service,
                            count=len(trigger_matches),
                        )
                    declared_dependencies_by_service = extract_declared_dependencies(
                        from_dicts=[trigger_matches[0]],
                    )

        # Build recommendations
        recommendations = _generate_recommendations(chain, snapshot_data)

        now = datetime.now(UTC)
        duration = snapshot_data.get("window", {}).get("duration_seconds", 0)

        data: dict[str, Any] = {
            "correlation_snapshot_id": snapshot.get("id"),
            "duration_minutes": duration / 60 if duration else 0,
            "decisions_affected": sum(1 for v in chain if v.get("type") == "quality_breach"),
            "verdict_count": len(chain),
            "root_cause": root_cause,
            "blast_radius": snapshot_data.get("affected_services", []),
            "timeline": timeline[:20],
            "recommendations": recommendations,
            "outcome_coverage": {
                "resolved": resolved_count,
                "pending": pending_count,
                "total": len(chain),
            },
        }
        if trigger_service is not None:
            data["trigger_service"] = trigger_service
        if declared_dependencies_by_service is not None:
            data["declared_dependencies_by_service"] = declared_dependencies_by_service

        assessment = {
            "id": f"retro-{service}-{uuid.uuid4().hex[:8]}",
            "created_at": now.isoformat(),
            "kind": "retrospective",
            "service": service,
            "data": data,
        }

        result = await self.client.submit_assessment(wrap_assessment(assessment, component="learn"))
        if not result.ok:
            logger.warning("retrospective_submit_failed", service=service, error=result.error)
            return False
        logger.info("retrospective_emitted", service=service, verdict_count=len(chain))
        return True

    async def get_state(self) -> dict:
        state: dict[str, Any] = {}
        if self._cursor:
            state["cursor"] = self._cursor
        return state


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _build_chain_timeline(chain: list[dict]) -> list[dict]:
    """Build a chronological timeline from a verdict chain."""
    entries = []
    for v in sorted(chain, key=lambda x: x.get("created_at", "")):
        entries.append({
            "timestamp": v.get("created_at"),
            "type": v.get("type", "unknown"),
            "service": v.get("service", "unknown"),
            "id": v.get("id"),
            "outcome": v.get("outcome", {}).get("status"),
        })
    return entries


def _generate_recommendations(chain: list[dict], snapshot_data: dict) -> list[dict]:
    """Generate recommendations from a verdict chain and snapshot data."""
    recommendations: list[dict] = []

    # SLO gate recommendation if quality_breach found
    breaches = [v for v in chain if v.get("type") == "quality_breach"]
    if breaches:
        recommendations.append({
            "type": "slo_gate",
            "detail": "Block model deploys when judgment SLO is breached",
        })

    # Dependency review if blast radius is large
    blast = snapshot_data.get("affected_services", [])
    if len(blast) > 3:
        recommendations.append({
            "type": "dependency_review",
            "detail": f"Blast radius of {len(blast)} services suggests tight coupling",
        })

    # Change control if multiple verdicts in short time
    if len(chain) > 5:
        recommendations.append({
            "type": "change_control",
            "detail": "High verdict volume suggests insufficient change control",
        })

    return recommendations
