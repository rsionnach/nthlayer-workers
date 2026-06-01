# src/nthlayer_respond/coordinator.py
"""Coordinator state machine — pure transport, no judgment.

Sequences the agent pipeline, persists context after each step,
handles crash recovery via last_completed_step_index, and gates
on escalation / human approval.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from nthlayer_workers.respond.types import (
    TERMINAL_STATES,
    AgentRole,
    IncidentContext,
    IncidentState,
)

logger = structlog.get_logger(__name__)

# Step 0: triage (serial)
# Step 1: investigation + communication (parallel)
# Step 2: remediation (serial)
# Step 3: communication resolution update (serial)
PIPELINE: list[list[AgentRole]] = [
    [AgentRole.TRIAGE],
    [AgentRole.INVESTIGATION, AgentRole.COMMUNICATION],
    [AgentRole.REMEDIATION],
    [AgentRole.COMMUNICATION],
]

# Map from pipeline step index to the state the coordinator should set
# before running that step.
_STEP_STATES: dict[int, IncidentState] = {
    0: IncidentState.TRIAGING,
    1: IncidentState.INVESTIGATING,
    2: IncidentState.REMEDIATING,
    3: IncidentState.REMEDIATING,  # still remediating phase for resolution update
}


def _build_approval_custom(
    action: str | None,
    target: str | None,
    approved_by: str | None,
) -> dict[str, Any]:
    """Build the metadata.custom dict for an approval-step verdict.

    All three keys are always present — success and failure paths and
    authenticated/unauthenticated callers all produce the same shape so
    downstream consumers (bench brief, post-incident review) can pattern-match
    on a fixed key set without defensive ``in`` checks. ``approved_by``
    defaults to ``"human"`` when absent, mirroring the reasoning string's
    fallback (``f"{who} approved {action} on {target}"`` where
    ``who = approved_by or "human"``).
    """
    return {
        "proposed_action": action,
        "target": target,
        "approved_by": approved_by or "human",
    }


class Coordinator:
    """Deterministic state machine that sequences agent execution.

    Not an agent — has no model access.  Pure transport: receives context,
    runs the pipeline, persists state, checks gates.
    """

    def __init__(
        self,
        agents: dict[AgentRole, Any],
        context_store: Any,
        verdict_store: Any,
        config: Any,
        safe_action_registry: Any | None = None,
        escalation_runner: Any | None = None,
    ) -> None:
        self._agents = agents
        self._context_store = context_store
        self._verdict_store = verdict_store
        self._config = config
        self._registry = safe_action_registry
        self._escalation_runner = escalation_runner

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    async def run(self, context: IncidentContext) -> IncidentContext:
        """Execute the agent pipeline from the current step to completion.

        On success: state -> RESOLVED.
        On escalation gate: state -> ESCALATED.
        On human approval gate: state -> AWAITING_APPROVAL.
        On unrecoverable error: state -> FAILED.
        """
        try:
            return await self._run_pipeline(context)
        except Exception as exc:  # noqa: BLE001
            logger.error("coordinator_unrecoverable", error=str(exc))
            context.state = IncidentState.FAILED
            # P3-E.1: <reason>: <details> error format convention
            context.error = f"unrecoverable: {exc}"
            self._context_store.save(context)
            return context

    async def resume(self, incident_id: str) -> IncidentContext:
        """Load a persisted context and continue the pipeline."""
        context = self._context_store.load(incident_id)
        if context is None:
            raise ValueError(f"Incident {incident_id!r} not found in context store")
        return await self.run(context)

    async def approve(self, incident_id: str, approved_by: str | None = None) -> IncidentContext:
        """Execute the approved safe action for a paused incident.

        Requires state == AWAITING_APPROVAL.
        On success: state -> RESOLVED.
        On failure: state -> ESCALATED.

        Args:
            incident_id: Incident to approve.
            approved_by: Identity of the approver (e.g. email). Stored in
                verdict metadata and reasoning for auditability. Defaults to
                "human" when not provided.
        """
        context = self._context_store.load(incident_id)
        if context is None:
            raise ValueError(f"Incident {incident_id!r} not found in context store")
        if context.state != IncidentState.AWAITING_APPROVAL:
            raise ValueError(
                f"Incident {incident_id!r} is in state {context.state.value}, "
                f"not AWAITING_APPROVAL"
            )

        remediation = context.remediation
        if remediation is None:
            raise ValueError(
                f"Incident {incident_id!r} has no remediation result to approve"
            )
        action = remediation.proposed_action
        target = remediation.target

        if self._registry is None:
            raise ValueError(
                f"Incident {incident_id!r}: no safe action registry configured on coordinator"
            )
        registry = self._registry

        who = approved_by or "human"
        from nthlayer_common.verdicts import create as verdict_create

        # Bead 1: structured fields for downstream consumers (bench brief,
        # post-incident review). Identical shape on success and failure paths
        # so the next reader can confirm at a glance the only difference is
        # the verdict's subject/judgment, not its metadata.
        approval_custom = _build_approval_custom(action, target, approved_by)

        try:
            exec_result = await registry.execute(action, target, context)
            remediation.executed = True
            remediation.execution_result = exec_result.get("detail", "")

            v = verdict_create(
                subject={
                    "type": "remediation",
                    "ref": context.id,
                    "summary": f"approved: {action} on {target}",
                },
                judgment={
                    "action": "approve",
                    "confidence": 1.0,
                    "reasoning": f"{who} approved {action} on {target}",
                },
                producer={"system": "nthlayer-respond", "instance": "coordinator"},
                metadata={"custom": approval_custom},
            )
            # opensrm-saun.1.2: typed column matches subject.type. RBAC §10's
            # "approval" verdict-type is a v2 concept (separate authorise
            # module); v1.5's coordinator owns the bundled approve-and-execute
            # flow, so the verdict's nature is "remediation". The approve
            # vs deny distinction lives in judgment.action.
            v.verdict_type = "remediation"
            self._verdict_store.put(v)
            context.verdict_chain.append(v.id)

            context.state = IncidentState.RESOLVED
            self._context_store.save(context)
            return context

        except Exception as exc:  # noqa: BLE001
            logger.error("approve_execution_failed", error=str(exc))

            v = verdict_create(
                subject={
                    "type": "remediation",
                    "ref": context.id,
                    "summary": f"approval failed: {action} on {target}",
                },
                judgment={
                    "action": "escalate",
                    "confidence": 0.0,
                    "reasoning": f"Approved action failed: {exc}",
                },
                producer={"system": "nthlayer-respond", "instance": "coordinator"},
                metadata={"custom": approval_custom},
            )
            # See approval-success branch above for the rationale on
            # remediation vs approval/denial typing.
            v.verdict_type = "remediation"
            self._verdict_store.put(v)
            context.verdict_chain.append(v.id)

            context.state = IncidentState.ESCALATED
            self._context_store.save(context)
            return context

    async def reject(
        self, incident_id: str, reason: str, rejected_by: str | None = None
    ) -> IncidentContext:
        """Reject a proposed remediation action.

        Requires state == AWAITING_APPROVAL.
        Resolves the last remediation verdict as "overridden" and sets
        state -> ESCALATED.

        Args:
            incident_id: Incident to reject.
            reason: Human-readable reason for rejection.
            rejected_by: Identity of the rejector (e.g. email). Stored in
                the override reasoning for auditability. Defaults to "human"
                when not provided.
        """
        context = self._context_store.load(incident_id)
        if context is None:
            raise ValueError(f"Incident {incident_id!r} not found in context store")
        if context.state != IncidentState.AWAITING_APPROVAL:
            raise ValueError(
                f"Incident {incident_id!r} is in state {context.state.value}, "
                f"not AWAITING_APPROVAL"
            )

        remediation = context.remediation
        proposed_action = remediation.proposed_action if remediation else "unknown"
        target = remediation.target if remediation else "unknown"

        who = rejected_by or "human"

        # Resolve the last verdict in the chain as overridden
        if context.verdict_chain:
            last_verdict_id = context.verdict_chain[-1]
            try:
                self._verdict_store.resolve(
                    last_verdict_id,
                    "overridden",
                    override={
                        "by": who,
                        "reasoning": (
                            f"{who} rejected {proposed_action} of {target}: {reason}"
                        ),
                    },
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "reject_verdict_resolve_failed",
                    verdict_id=last_verdict_id,
                    error=str(exc),
                )

        context.state = IncidentState.ESCALATED
        self._context_store.save(context)
        return context

    # ------------------------------------------------------------------ #
    # Internal pipeline execution                                          #
    # ------------------------------------------------------------------ #

    async def _run_pipeline(self, context: IncidentContext) -> IncidentContext:
        """Walk through pipeline steps, running agents and checking gates."""
        # Worker-mode invariant: validate state on entry. AWAITING_APPROVAL is a
        # non-progressable wait state under polling-driven invocation; without
        # this guard, _next_step() would return 3 (since last_completed_step_index
        # = 2 at the gate) and step 3 would proceed, bypassing the approval gate.
        # Resumption from AWAITING_APPROVAL is P3-E.3's approval-verdict polling
        # path; in P3-E.1, AWAITING_APPROVAL incidents simply wait.
        if context.state == IncidentState.AWAITING_APPROVAL:
            return context

        start_step = self._next_step(context)
        if start_step is None:
            # Already complete
            if context.state not in TERMINAL_STATES:
                context.state = IncidentState.RESOLVED
                self._context_store.save(context)
            return context

        for step_index in range(start_step, len(PIPELINE)):
            step_roles = PIPELINE[step_index]

            # Before step 3 (second communication): skip if escalated or failed
            if step_index == 3 and context.state in {
                IncidentState.ESCALATED,
                IncidentState.FAILED,
            }:
                break

            # Update state to reflect current phase
            new_state = _STEP_STATES.get(step_index)
            if new_state is not None:
                context.state = new_state

            # Execute step (P3-E.1: bounded by step_timeout_seconds when set on config)
            timeout = self._step_timeout()
            try:
                if len(step_roles) == 1:
                    coro = self._run_serial_step(context, step_roles[0])
                else:
                    coro = self._run_parallel_step(context, step_roles)
                if timeout is not None:
                    await asyncio.wait_for(coro, timeout=timeout)
                else:
                    await coro
            except TimeoutError:
                step_label = (
                    step_roles[0].value
                    if len(step_roles) == 1
                    else "+".join(r.value for r in step_roles)
                )
                context.state = IncidentState.FAILED
                context.error = f"step_timeout: {step_label} exceeded {timeout}s"
                self._context_store.save(context)
                return context

            # Persist after step
            context.last_completed_step_index = step_index
            self._context_store.save(context)

            # After triage (step 0): fire on-call escalation if configured
            if step_index == 0 and self._escalation_runner is not None:
                await self._maybe_start_escalation(context)

            # Gate: escalation check
            if self._check_escalation(context):
                context.state = IncidentState.ESCALATED
                self._context_store.save(context)
                return context

            # Gate: human approval (after remediation step, index 2)
            if (
                step_index == 2
                and context.remediation is not None
                and context.remediation.requires_human_approval
            ):
                context.state = IncidentState.AWAITING_APPROVAL
                context.updated_at = datetime.now(UTC).isoformat()
                self._context_store.save(context)
                return context

        # All steps complete
        if context.state not in TERMINAL_STATES:
            context.state = IncidentState.RESOLVED
            self._context_store.save(context)

        return context

    async def _run_serial_step(
        self, context: IncidentContext, role: AgentRole
    ) -> None:
        """Run a single agent synchronously."""
        agent = self._agents[role]
        logger.info("step_start", role=role.value, incident=context.id)
        await agent.execute(context)
        logger.info("step_complete", role=role.value, incident=context.id)

    async def _run_parallel_step(
        self, context: IncidentContext, roles: list[AgentRole]
    ) -> None:
        """Run multiple agents in parallel via asyncio.gather.

        Each agent writes to a different field on context, so no data race.
        Investigation failure is critical; communication failure is non-blocking.
        """
        tasks = [self._agents[role].execute(context) for role in roles]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for role, result in zip(roles, results):
            if isinstance(result, Exception):
                if role == AgentRole.INVESTIGATION:
                    logger.error(
                        "investigation_failed",
                        error=str(result),
                        incident=context.id,
                    )
                else:
                    logger.warning(
                        "communication_failed",
                        error=str(result),
                        incident=context.id,
                    )

    # ------------------------------------------------------------------ #
    # On-call escalation                                                   #
    # ------------------------------------------------------------------ #

    async def _maybe_start_escalation(self, context: IncidentContext) -> None:
        """Fire the escalation runner if the manifest has an oncall config.

        Fail-open: escalation failure never blocks the incident pipeline.
        """
        from nthlayer_workers.respond.notification_backends.protocol import NotificationPayload
        from nthlayer_workers.respond.oncall.escalation import EscalationStep

        try:
            svc_ctx = context.metadata.get("service_context", {})
            oncall = (
                svc_ctx.get("spec", {})
                .get("ownership", {})
                .get("oncall")
            )
            if not oncall:
                return

            # Parse escalation steps from manifest config
            raw_steps = oncall.get("escalation", [])
            if not raw_steps:
                return

            steps = []
            for raw in raw_steps:
                after_str = raw["after"]
                if not after_str.endswith("m") or not after_str[:-1].isdigit():
                    logger.warning(
                        "escalation_step_invalid_after",
                        after=after_str,
                        incident_id=context.id,
                    )
                    continue
                minutes = int(after_str[:-1])
                steps.append(
                    EscalationStep(
                        after=timedelta(minutes=minutes),
                        notify=raw["notify"],
                        target=raw.get("target"),
                        phone=raw.get("phone"),
                    )
                )

            severity = getattr(context.triage, "severity", 3) if context.triage else 3
            title = context.triage.reasoning[:80] if context.triage and context.triage.reasoning else context.id

            payload = NotificationPayload(
                incident_id=context.id,
                severity=severity,
                title=title,
                summary=context.triage.reasoning if context.triage else "Incident triggered",
                root_cause=None,
                blast_radius=list(context.triage.blast_radius) if context.triage else [],
                actions_url=None,
                escalation_step=0,
                requires_ack=True,
            )

            await self._escalation_runner.start_escalation(
                incident_id=context.id,
                payload=payload,
                steps=steps,
            )
            logger.info(
                "escalation_triggered",
                incident_id=context.id,
                steps=len(steps),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "escalation_start_failed",
                incident_id=context.id,
                error=str(exc),
            )

    # ------------------------------------------------------------------ #
    # Gates                                                                #
    # ------------------------------------------------------------------ #

    def _check_escalation(self, context: IncidentContext) -> bool:
        """Return True if any agent has flagged this incident for escalation.

        P3-E.1: capture-at-write-time. The flag is set inside ``_emit_verdict``
        (agents/base.py) when a verdict has ``action=escalate`` and
        ``confidence < escalation_threshold``. The gate now reads a single
        boolean from context.metadata — no verdict-store re-fetch, no
        per-step API round-trip in worker mode.
        """
        return bool(context.metadata.get("escalation_pending", False))

    def _step_timeout(self) -> float | None:
        """Return the per-step timeout, or None if not configured.

        Defensive against test mocks that don't set the field. Returns the
        configured value only when it is a positive number; otherwise None
        (no timeout, used by older tests with MagicMock configs).
        """
        val = getattr(self._config, "step_timeout_seconds", None)
        if isinstance(val, (int, float)) and val > 0:
            return float(val)
        return None

    @staticmethod
    def _next_step(context: IncidentContext) -> int | None:
        """Determine the next pipeline step to execute.

        Returns None if all steps are complete.
        """
        if context.last_completed_step_index is None:
            return 0
        next_idx = context.last_completed_step_index + 1
        if next_idx >= len(PIPELINE):
            return None
        return next_idx
