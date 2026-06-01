"""RespondModule — worker module driving incident response (P3-E.1).

Polls correlation_snapshot assessments (primary) and quality_breach verdicts
(fallback) from core via CoreAPIClient. Opens incidents, runs the agent
pipeline via Coordinator, submits verdicts back to core, persists incident
state to component_state for crash recovery.

The worker is the sole owner of in-memory incident state. Agents and the
coordinator have NO back-reference to the worker — `self._incidents` is
mutated only by `_drive_active_incidents` after `coordinator.run(context)`
completes for each incident, and by `_ingest_triggers` before the drive phase.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import structlog
from nthlayer_common.api_client import CoreAPIClient

from nthlayer_workers.respond.config import RespondConfig
from nthlayer_workers.respond.context_store import (
    incident_context_from_dict,
    incident_context_to_dict,
)
from nthlayer_workers.respond.coordinator import Coordinator
from nthlayer_workers.respond.types import (
    TERMINAL_STATES,
    IncidentContext,
    IncidentState,
)
from nthlayer_workers.respond.worker_helpers import (
    breach_ids_with_snapshots,
    filter_after,
    open_from_breach,
    open_from_snapshot,
)

logger = structlog.get_logger(__name__)


@dataclass
class Cursors:
    """Polling cursors for trigger ingestion. Persisted via component_state."""
    snapshot_after: str | None = None
    breach_after: str | None = None

    def to_dict(self) -> dict:
        return {
            "snapshot_after": self.snapshot_after,
            "breach_after": self.breach_after,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Cursors:
        return cls(
            snapshot_after=data.get("snapshot_after"),
            breach_after=data.get("breach_after"),
        )


def _is_terminal_and_aged(context: IncidentContext, retention_seconds: float) -> bool:
    """True if context is in a terminal state AND older than retention window.

    Terminal states (RESOLVED/ESCALATED/FAILED) are kept for retention_seconds
    so operators can inspect closed incidents; older terminal incidents are
    pruned on the next get_state() call.
    """
    if context.state not in TERMINAL_STATES:
        return False
    try:
        # updated_at is ISO 8601 (set by coordinator and ingest paths)
        updated = datetime.fromisoformat(context.updated_at.replace("Z", "+00:00"))
        if updated.tzinfo is None:
            # Naive datetime — treat as UTC for the subtraction. Without this
            # the next line raises TypeError ("can't subtract offset-naive
            # and offset-aware datetimes"), which would block every save.
            updated = updated.replace(tzinfo=UTC)
        age = (datetime.now(UTC) - updated).total_seconds()
    except (ValueError, AttributeError, TypeError) as exc:
        # Malformed timestamp → treat as not-aged so we don't drop accidentally.
        # Debug-level so it's invisible until needed for diagnosis.
        logger.debug(
            "respond_terminal_age_check_skipped_malformed_timestamp",
            updated_at=context.updated_at,
            error=str(exc),
        )
        return False
    return age >= retention_seconds


class _NoopContextStore:
    """Satisfies the coordinator's context_store interface without persisting.

    Worker mode owns persistence at the cycle boundary, not per-step. The
    coordinator's existing save(context) calls become no-ops here. Reads
    are NEVER expected in worker mode — the worker holds the live
    IncidentContext in self._incidents — so load() and list_active() raise
    loudly rather than returning silent-wrong-answer values like None or [].
    Loud failure on misuse beats invisible bugs.
    """
    def save(self, context: IncidentContext) -> None:
        pass

    def load(self, incident_id: str) -> IncidentContext | None:
        raise NotImplementedError(
            "worker mode reads from RespondModule._incidents directly, not context_store"
        )

    def list_active(self) -> list[str]:
        raise NotImplementedError(
            "worker mode reads from RespondModule._incidents directly, not context_store"
        )

    def list_all(self, limit: int = 50) -> list[IncidentContext]:
        raise NotImplementedError(
            "worker mode reads from RespondModule._incidents directly, not context_store"
        )

    def get_metadata(self, key: str) -> str | None:
        raise NotImplementedError("worker mode does not use context_store metadata")

    def set_metadata(self, key: str, value: str) -> None:
        raise NotImplementedError("worker mode does not use context_store metadata")

    def close(self) -> None:
        pass


@dataclass
class RespondModule:
    """Respond worker module — drives incidents from triggers polled from core API.

    Reads correlation_snapshot assessments (primary) and quality_breach verdicts
    (fallback) from core, opens incident contexts, runs the agent pipeline,
    submits verdicts back to core, persists incident state to component_state.

    **Single-instance contract (v1.5):** RespondModule does NOT acquire a lease
    on triggers or incidents. Two RespondModule instances polling the same core
    will both ingest the same snapshots/breaches and open duplicate incidents
    (different INC-... ids, identical trigger_verdict_ids). v1.5 deployments
    must run exactly one workers process per core. Multi-instance HA is a v2
    concern (see spec "Out of P3-E.1 scope" — case-API integration covers
    lease-based ownership for incidents).
    """
    client: CoreAPIClient
    config: RespondConfig
    deployment_id: str | None = None

    # In-memory mirrors of component_state, restored on restore_state()
    _cursors: Cursors = field(default_factory=Cursors)
    _incidents: dict[str, IncidentContext] = field(default_factory=dict)

    # Lazy: agents and coordinator constructed on first drive cycle. Tests
    # may pre-set these via _agents/_coordinator to inject mocks.
    _agents: dict | None = None
    _coordinator: Coordinator | None = None

    @property
    def name(self) -> str:
        # Single-module form for v1.5. If the spec's planned split into
        # RespondTriggerModule (fast trigger detection) and RespondPipelineModule
        # (slow pipeline drive) is implemented, rename to "respond.pipeline" to
        # match the observe.{collect,drift,topology} / correlate.{session,topology,
        # contract} naming convention used elsewhere in workers.
        return "respond"

    async def restore_state(self, state: dict | None) -> None:
        """Reload cursors and active incidents from component_state."""
        if state is None:
            return
        self._cursors = Cursors.from_dict(state.get("cursors") or {})
        for incident_id, raw in (state.get("incidents") or {}).items():
            try:
                self._incidents[incident_id] = incident_context_from_dict(raw)
            except (KeyError, ValueError, TypeError):
                logger.warning(
                    "respond_state_restore_skipped_corrupt_incident",
                    incident_id=incident_id,
                )

    async def process_cycle(self) -> None:
        """One cycle: detect new triggers → drive active incidents.

        State is persisted by the runner via get_state() after this returns.
        """
        await self._ingest_triggers()
        await self._drive_active_incidents()

    # ------------------------------------------------------------------ #
    # Pipeline drive                                                        #
    # ------------------------------------------------------------------ #

    def _ensure_coordinator(self) -> Coordinator:
        """Lazily construct agents + coordinator on first drive cycle."""
        if self._coordinator is not None:
            return self._coordinator

        if self._agents is None:
            self._agents = self._build_agents()

        self._coordinator = Coordinator(
            agents=self._agents,
            context_store=_NoopContextStore(),
            verdict_store=None,  # worker mode: agents submit via CoreAPIClient
            config=self.config,
            safe_action_registry=None,  # P3-E.3
            escalation_runner=None,     # P3-E.5
        )
        return self._coordinator

    def _build_agents(self) -> dict:
        """Construct the agent stack with worker-mode (client-based) plumbing."""
        # Local imports to avoid pulling agent modules at import time of
        # respond.worker (keeps test collection fast and avoids prompt-file
        # FileNotFoundError on environments where prompts aren't bundled).
        from nthlayer_workers.respond.agents.communication import CommunicationAgent
        from nthlayer_workers.respond.agents.investigation import InvestigationAgent
        from nthlayer_workers.respond.agents.remediation import RemediationAgent
        from nthlayer_workers.respond.agents.triage import TriageAgent
        from nthlayer_workers.respond.types import AgentRole

        agent_config = {
            "root_cause_threshold": self.config.root_cause_threshold,
            "arbiter_url": self.config.arbiter_url,
            "escalation_threshold": self.config.escalation_threshold,
        }
        kwargs = {
            "client": self.client,
            "deployment_id": self.deployment_id,
        }
        return {
            AgentRole.TRIAGE: TriageAgent(
                self.config.model, self.config.max_tokens, None, agent_config,
                timeout=self.config.triage_timeout, **kwargs,
            ),
            AgentRole.INVESTIGATION: InvestigationAgent(
                self.config.model, self.config.max_tokens, None, agent_config,
                timeout=self.config.investigation_timeout, **kwargs,
            ),
            AgentRole.COMMUNICATION: CommunicationAgent(
                self.config.model, self.config.max_tokens, None, agent_config,
                timeout=self.config.communication_timeout, **kwargs,
            ),
            AgentRole.REMEDIATION: RemediationAgent(
                self.config.model, self.config.max_tokens, None, agent_config,
                timeout=self.config.remediation_timeout,
                safe_action_registry=None,  # P3-E.3 wires the registry
                **kwargs,
            ),
        }

    async def _drive_active_incidents(self) -> None:
        """Run each non-terminal, non-AWAITING_APPROVAL incident through the
        coordinator pipeline.

        AWAITING_APPROVAL incidents are skipped — coordinator's _run_pipeline
        also early-returns on that state, but skipping here avoids the call
        entirely. Resumption (poll for approval verdict) is P3-E.3.

        Incidents in TERMINAL_STATES are skipped (they only need terminal
        retention pruning, handled in get_state()).
        """
        # Snapshot the incident IDs to avoid mutation-during-iteration if
        # _ingest_triggers were ever extended to run concurrently with this.
        # Today the cycle is sequential, but the snapshot makes the invariant
        # explicit.
        incident_ids = list(self._incidents.keys())
        if not incident_ids:
            return

        coordinator = self._ensure_coordinator()
        for incident_id in incident_ids:
            context = self._incidents[incident_id]
            if context.state in TERMINAL_STATES:
                continue
            if context.state == IncidentState.AWAITING_APPROVAL:
                continue
            try:
                updated = await coordinator.run(context)
                self._incidents[incident_id] = updated
            except Exception:  # noqa: BLE001
                logger.exception(
                    "respond_incident_drive_failed",
                    incident_id=incident_id,
                )
                # coordinator.run already catches inner exceptions and sets
                # FAILED. This catch is for unexpected escapes (e.g. errors
                # during coordinator construction). Mark FAILED so the
                # incident doesn't get retried indefinitely.
                context.state = IncidentState.FAILED
                context.error = "unrecoverable: coordinator.run escape"
                self._incidents[incident_id] = context

    # ------------------------------------------------------------------ #
    # Trigger ingestion                                                     #
    # ------------------------------------------------------------------ #

    async def _ingest_triggers(self) -> None:
        """Detect new triggers and open incidents.

        Primary path: correlation_snapshot assessments (situation-shaped).
        Fallback path: quality_breach verdicts older than fallback_threshold
        without an associated correlation_snapshot referencing them.

        Snapshots are fetched once and reused for the fallback dedup check —
        avoids the per-breach API fan-out that would otherwise occur.
        """
        snapshot_cache = await self._ingest_snapshots()
        await self._ingest_orphan_breaches(snapshot_cache)

    async def _ingest_snapshots(self) -> list[dict]:
        """Primary trigger path: correlation_snapshot assessments.

        Returns the snapshot page (already filtered by cursor) so the fallback
        path can dedup against it without re-fetching.
        """
        result = await self.client.get_assessments(kind="correlation_snapshot")
        if not result.ok:
            logger.warning(
                "respond_snapshot_poll_failed",
                status_code=result.status_code,
                error=result.error,
            )
            return []

        all_snapshots = result.data or []
        # Client-side filter by created_after the cursor. v1.5 limitation:
        # core's /assessments API filters by kind only. Mirrors P3-D.1's same
        # constraint in correlate.
        snapshots = filter_after(all_snapshots, self._cursors.snapshot_after)
        if not snapshots:
            return all_snapshots

        # Sort by created_at ascending so cursor advances monotonically.
        snapshots.sort(key=lambda s: s.get("created_at", ""))
        triggered = self._incidents_triggered_from()
        for snap in snapshots:
            snap_id = snap.get("id")
            created_at = snap.get("created_at")
            if not snap_id or not created_at:
                continue
            if snap_id not in triggered:
                try:
                    await self._open_incident_from_snapshot(snap)
                except Exception:  # noqa: BLE001
                    # Defensive: malformed snapshot must not stall the cursor
                    # forever (would block all subsequent snapshots in the
                    # batch and on every later cycle).
                    logger.exception(
                        "respond_snapshot_open_failed",
                        snap_id=snap_id,
                    )
            # Advance cursor whether opened, skipped (already triggered), or
            # failed (logged). Asymmetry-fix: only advance after the open
            # attempt, not before — matches the spec's restartable-on-failure
            # intent for the snapshot path.
            self._cursors.snapshot_after = created_at

        return all_snapshots

    async def _ingest_orphan_breaches(self, snapshot_cache: list[dict]) -> None:
        """Fallback trigger path: quality_breach verdicts without snapshot.

        For each new quality_breach verdict older than fallback_threshold_seconds:
        check whether a correlation_snapshot exists referencing it. If yes, skip
        (already represented). If no, open an incident from the breach directly.

        ``snapshot_cache`` is the per-cycle cache from _ingest_snapshots — used
        for parent_ids dedup without re-fetching once per breach. v1.5 limitation:
        if the snapshot list paginates beyond the page limit, snapshots beyond
        page 1 may be missed (logged as v2 follow-up: server-side parent_ids
        filter).

        Logs the threshold value separately so the metadata.fallback_reason
        string stays stable if the threshold becomes operationally configurable.
        """
        cutoff = datetime.now(UTC) - timedelta(
            seconds=self.config.fallback_threshold_seconds
        )
        cutoff_iso = cutoff.isoformat()
        # Guard against created_after > created_before, which is undefined at
        # the core API level. Can happen with clock skew, a stalled worker
        # whose cursor advanced past now-cutoff, or a manual cursor injection.
        if self._cursors.breach_after and self._cursors.breach_after >= cutoff_iso:
            return
        result = await self.client.get_verdicts(
            verdict_type="quality_breach",
            created_before=cutoff_iso,
            created_after=self._cursors.breach_after,
        )
        if not result.ok:
            logger.warning(
                "respond_breach_poll_failed",
                status_code=result.status_code,
                error=result.error,
            )
            return

        breaches = result.data or []
        if not breaches:
            return

        # Build the dedup set once from the snapshot cache, not per-breach.
        snapshotted_breach_ids = breach_ids_with_snapshots(snapshot_cache)

        breaches.sort(key=lambda b: b.get("created_at", ""))
        triggered = self._incidents_triggered_from()
        for breach in breaches:
            breach_id = breach.get("id")
            created_at = breach.get("created_at")
            if not breach_id or not created_at:
                continue
            try:
                if breach_id in triggered:
                    pass  # already opened in a prior cycle
                elif breach_id in snapshotted_breach_ids:
                    pass  # represented by a snapshot incident — fallback skipped
                else:
                    opened_id = await self._open_incident_from_breach(breach)
                    logger.info(
                        "respond_fallback_trigger_used",
                        incident_id=opened_id,
                        breach_id=breach_id,
                        fallback_threshold_seconds=self.config.fallback_threshold_seconds,
                    )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "respond_breach_open_failed",
                    breach_id=breach_id,
                )
            # Advance cursor after every breach (opened, skipped, or failed)
            # so a single bad record doesn't permanently stall the cursor.
            self._cursors.breach_after = created_at

    def _incidents_triggered_from(self) -> set[str]:
        """Union of trigger_verdict_ids across all active incidents.

        Used to dedup snapshots and breaches if cursor advancement hiccups
        (e.g. process restart with stale cursor). A trigger we've already
        opened an incident for must not produce another incident.
        """
        seen: set[str] = set()
        for ctx in self._incidents.values():
            seen.update(ctx.trigger_verdict_ids)
        return seen

    # ------------------------------------------------------------------ #
    # Incident opening                                                      #
    # ------------------------------------------------------------------ #

    async def _open_incident_from_snapshot(self, snap: dict) -> str:
        """Open an incident from a correlation_snapshot assessment.

        Resolves real service tiers from manifests via the core API so the
        triage agent's severity derivation is grounded in declared tier rather
        than a hardcoded placeholder.
        """
        data = snap.get("data") or {}
        affected = data.get("affected_services") or [
            data.get("domain", {}).get("service") or "unknown"
        ]
        tiers = await self._resolve_tiers(affected)
        ctx = open_from_snapshot(snap, self.config, tiers=tiers)
        self._incidents[ctx.id] = ctx

        # Eager case creation: anchor on the breach verdict id so bench's
        # lineage walk resolves; skip silently when the snapshot has no
        # parent_ids (logged for operator visibility).
        # See docs/superpowers/decisions/eager-case-creation.md
        breach_ids = data.get("parent_ids") or []
        if not breach_ids:
            logger.warning(
                "respond_case_create_skipped_no_anchor",
                incident_id=ctx.id,
                snapshot_id=snap.get("id"),
            )
            return ctx.id
        service = data.get("domain", {}).get("service") or affected[0]
        environment = data.get("domain", {}).get("environment") or "production"
        briefing = (
            data.get("nl_summary", {}).get("summary")
            if isinstance(data.get("nl_summary"), dict)
            else None
        ) or f"Correlation snapshot: {data.get('event_count', 0)} event(s) on {service}"
        await self._create_case_for_incident(
            ctx,
            underlying_verdict=breach_ids[0],
            service=service,
            environment=environment,
            briefing=briefing,
        )
        return ctx.id

    async def _open_incident_from_breach(self, breach: dict) -> str:
        """Open an incident from a quality_breach verdict (fallback path)."""
        service = (
            breach.get("service")
            or (breach.get("subject") or {}).get("service")
            or "unknown"
        )
        tiers = await self._resolve_tiers([service])
        ctx = open_from_breach(breach, self.config, tiers=tiers)
        self._incidents[ctx.id] = ctx

        # Fallback path: anchor directly on the breach verdict (no snapshot
        # exists when correlate is degraded).
        # See docs/superpowers/decisions/eager-case-creation.md
        breach_data = breach.get("data") or {}
        slo_name = breach_data.get("slo_name", "unknown")
        # Quality_breach verdicts are emitted by measure as raw dicts (no
        # judgment.reasoning). Briefing is composed from breach.data fields
        # — richer content lands once the triage verdict completes and the
        # bench brief logic walks descendants.
        briefing = (
            f"{slo_name} breach on {service}: "
            f"{breach_data.get('current_value', '?')} vs target "
            f"{breach_data.get('target', '?')}"
        )
        await self._create_case_for_incident(
            ctx,
            underlying_verdict=breach["id"],
            service=service,
            environment="production",
            briefing=briefing,
        )
        return ctx.id

    async def _create_case_for_incident(
        self,
        ctx: IncidentContext,
        *,
        underlying_verdict: str,
        service: str,
        environment: str,
        briefing: str,
    ) -> None:
        """Eagerly POST a case to core when an incident opens.

        Design choice — eager (not lazy): cases are created as soon as
        respond knows about an incident, not after triage produces a
        verdict. Operators want incidents in the bench queue immediately
        so they can see what the system is working on; richer briefing
        content arrives later when the brief logic walks descendants of
        ``case.underlying_verdict``.

        Failure is non-fatal: if core rejects or is unreachable the cycle
        continues and the incident proceeds through the agent pipeline.
        Operators may not see this incident in the bench queue, but the
        verdict chain is still correct.
        """
        case = {
            "id": f"case-{ctx.id}",
            "kind": "incident",
            "created_at": ctx.created_at,
            "underlying_verdict": underlying_verdict,
            "service": service,
            "blast_radius": environment,  # str, used by core's _derive_priority
            "has_active_incident": True,
            "briefing": briefing,
        }
        result = await self.client.create_case(case)
        if not result.ok:
            logger.warning(
                "respond_case_create_failed",
                incident_id=ctx.id,
                status_code=result.status_code,
                error=result.error,
            )

    async def _resolve_tiers(self, services: list[str]) -> dict[str, str]:
        """Fetch tiers for the given services from core's manifest catalogue.

        Returns a service -> tier mapping. Services with no manifest, manifest
        fetch failures, or transport-level exceptions all fall back to
        ``"standard"`` so triage still receives a reasonable default — a
        manifest-fetch failure must not prevent an incident from being opened
        (drop-the-trigger would be a worse outcome than degrade-to-standard).
        Failures are logged so operators can spot manifest registration gaps.
        """
        tiers: dict[str, str] = {}
        for svc in services:
            if not svc or svc in tiers:
                continue
            try:
                result = await self.client.get_manifest(svc)
            except Exception:  # noqa: BLE001
                # CoreAPIClient is designed to never raise, but we defensively
                # degrade rather than propagate — a network blip during tier
                # lookup must not drop the incident.
                logger.warning(
                    "respond_manifest_lookup_exception_fallback_to_standard",
                    service=svc,
                    exc_info=True,
                )
                tiers[svc] = "standard"
                continue
            if result.ok and isinstance(result.data, dict):
                tiers[svc] = result.data.get("tier", "standard") or "standard"
            else:
                logger.info(
                    "respond_manifest_lookup_fallback_to_standard",
                    service=svc,
                    status_code=getattr(result, "status_code", None),
                )
                tiers[svc] = "standard"
        return tiers

    async def get_state(self) -> dict:
        """Return cursors + active incidents (with terminal-state pruning).

        Pruning rule: incidents in TERMINAL_STATES whose updated_at is older
        than config.terminal_retention_seconds are dropped. Active incidents
        are never pruned regardless of age.
        """
        retention = self.config.terminal_retention_seconds
        return {
            "cursors": self._cursors.to_dict(),
            "incidents": {
                inc_id: incident_context_to_dict(ctx)
                for inc_id, ctx in self._incidents.items()
                if not _is_terminal_and_aged(ctx, retention)
            },
        }
