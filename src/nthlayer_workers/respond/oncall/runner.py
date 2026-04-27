"""Escalation runner — drives the escalation state machine.

Called by the respond coordinator when an incident is created.
Executes due steps, dispatches to notification backends, and waits
for acknowledgment.  Does NOT run a background loop in tests —
the loop is started explicitly via ``start_escalation()``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import structlog

from nthlayer_workers.respond.notification_backends.protocol import (
    NotificationPayload,
)
from nthlayer_workers.respond.oncall.escalation import (
    EscalationState,
    EscalationStatus,
    EscalationStep,
)
from nthlayer_workers.respond.oncall.schedule import RosterMember, resolve_oncall

logger = structlog.get_logger(__name__)


class EscalationRunner:
    """Drives the escalation state machine.

    Holds a dict of notification backends keyed by channel name.
    When an escalation starts, the runner fires due steps immediately
    (step 0 with after=0m) and stores the state. In production the
    ``_run_loop`` background task checks for due steps periodically.
    """

    def __init__(
        self,
        backends: dict[str, Any],  # str → NotificationBackend
        oncall_config: dict,
        slack_channel: str | None = None,
    ) -> None:
        self.backends = backends
        self._oncall_config = oncall_config
        self._slack_channel = slack_channel
        self._active_escalations: dict[str, EscalationState] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    async def start_escalation(
        self,
        incident_id: str,
        payload: NotificationPayload,
        steps: list[EscalationStep],
    ) -> EscalationState:
        """Start a new escalation for an incident."""
        now = datetime.now(timezone.utc)
        state = EscalationState(
            incident_id=incident_id,
            started_at=now,
            steps=steps,
        )
        self._active_escalations[incident_id] = state

        # Fire any immediately due steps (after=0m)
        step = state.next_due_step(now)
        while step is not None:
            await self._execute_step(state, step, payload)
            step = state.next_due_step(now)

        # Start background loop for remaining steps
        if state.status == EscalationStatus.ACTIVE:
            task = asyncio.create_task(self._run_loop(state, payload))
            self._tasks[incident_id] = task

        logger.info(
            "escalation_started",
            incident_id=incident_id,
            steps_total=len(steps),
            steps_fired=state.current_step_index,
        )
        return state

    async def acknowledge(self, incident_id: str, user: str) -> None:
        """Acknowledge an escalation. Called from webhook handler."""
        state = self._active_escalations.get(incident_id)
        if state and state.status == EscalationStatus.ACTIVE:
            state.acknowledge(user, datetime.now(timezone.utc))
            self._cancel_task(incident_id)
            logger.info(
                "escalation_acknowledged",
                incident_id=incident_id,
                user=user,
            )

    async def _run_loop(
        self, state: EscalationState, payload: NotificationPayload
    ) -> None:
        """Background loop: check for due steps, dispatch, sleep."""
        while state.status == EscalationStatus.ACTIVE:
            now = datetime.now(timezone.utc)
            step = state.next_due_step(now)

            if step:
                await self._execute_step(state, step, payload)

            wait = state.time_until_next_step(now)
            if wait is None:
                break

            sleep_secs = min(wait.total_seconds(), 5.0)
            await asyncio.sleep(max(sleep_secs, 1.0))

        if state.status == EscalationStatus.EXHAUSTED:
            logger.warning(
                "escalation_exhausted",
                incident_id=state.incident_id,
                steps_executed=state.current_step_index,
            )

    async def _execute_step(
        self,
        state: EscalationState,
        step: EscalationStep,
        payload: NotificationPayload,
    ) -> None:
        """Execute a single escalation step."""
        oncall = resolve_oncall(self._oncall_config, datetime.now(timezone.utc))

        # slack_channel: post to team channel
        if step.notify == "slack_channel":
            if self._slack_channel and "slack_dm" in self.backends:
                slack = self.backends["slack_dm"]
                result = await slack.send_to_channel(self._slack_channel, payload)
                state.notifications_sent.append(result)
                logger.info(
                    "escalation_step_sent",
                    step=step.notify,
                    channel=self._slack_channel,
                )
            return

        # Determine target person
        if step.target == "next_oncall":
            recipient = oncall.secondary
        elif step.target == "engineering_manager":
            recipient = RosterMember(
                name="Engineering Manager",
                slack_id="",
                ntfy_topic=None,
                phone=step.phone,
            )
        else:
            recipient = oncall.primary

        # Dispatch to backend
        backend = self.backends.get(step.notify)
        if not backend:
            logger.warning(
                "escalation_backend_missing",
                step=step.notify,
                incident_id=state.incident_id,
            )
            return

        result = await backend.send(recipient, payload)
        state.notifications_sent.append(result)

        logger.info(
            "escalation_step_sent",
            step=step.notify,
            recipient=recipient.name,
            delivered=result.delivered,
            error=result.error,
        )

    def _cancel_task(self, incident_id: str) -> None:
        """Cancel the background loop task for an incident."""
        task = self._tasks.pop(incident_id, None)
        if task and not task.done():
            task.cancel()

    async def shutdown(self) -> None:
        """Cancel all active escalation tasks. Call on server shutdown."""
        tasks = []
        for incident_id in list(self._tasks):
            task = self._tasks.pop(incident_id, None)
            if task and not task.done():
                task.cancel()
                tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
