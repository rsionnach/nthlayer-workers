"""HTTP server for incident approval workflows.

Starlette ASGI app with routes for approve, reject, status, and
Slack interaction callbacks. Embedded in `nthlayer-respond serve`.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time as _time
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Any

from nthlayer_common.slack_web import SlackWebClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from nthlayer_workers.respond.config import RespondConfig
from nthlayer_workers.respond.metrics import VerdictMetricsCollector
from nthlayer_workers.respond.types import IncidentState

logger = logging.getLogger(__name__)


class ApprovalServer:
    """HTTP server for incident approval workflows."""

    def __init__(
        self,
        coordinator: Any,
        context_store: Any,
        config: RespondConfig,
        verdict_store: Any = None,
    ) -> None:
        self._coordinator = coordinator
        self._context_store = context_store
        self._config = config
        self._timeouts: dict[str, asyncio.Task] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._metrics = VerdictMetricsCollector(verdict_store) if verdict_store else None

    def _get_lock(self, incident_id: str) -> asyncio.Lock:
        """Get or create a per-incident lock to serialize approve/reject/timeout."""
        if incident_id not in self._locks:
            self._locks[incident_id] = asyncio.Lock()
        return self._locks[incident_id]

    @contextlib.asynccontextmanager
    async def _lifespan(self, app: Starlette) -> AsyncGenerator[None, None]:
        """Recover pending approval timeouts on startup."""
        await self.recover_pending_approvals()
        yield

    def build_app(self) -> Starlette:
        """Build the Starlette ASGI application."""
        routes = [
            Route(
                "/api/v1/incidents/{incident_id}/approve",
                self.handle_approve,
                methods=["POST"],
            ),
            Route(
                "/api/v1/incidents/{incident_id}/reject",
                self.handle_reject,
                methods=["POST"],
            ),
            Route(
                "/api/v1/incidents/{incident_id}",
                self.handle_status,
                methods=["GET"],
            ),
            Route(
                "/api/v1/slack/interactions",
                self.handle_slack_interaction,
                methods=["POST"],
            ),
            Route("/metrics", self.handle_metrics, methods=["GET"]),
        ]
        return Starlette(routes=routes, lifespan=self._lifespan)

    async def handle_approve(self, request: Request) -> JSONResponse:
        """POST /api/v1/incidents/{id}/approve"""
        incident_id = request.path_params["incident_id"]
        try:
            body = await request.json() if await request.body() else {}
        except (json.JSONDecodeError, ValueError):
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        approved_by = body.get("approved_by")

        async with self._get_lock(incident_id):
            try:
                ctx = await self._coordinator.approve(
                    incident_id, approved_by=approved_by
                )
            except ValueError as exc:
                msg = str(exc)
                if "not found" in msg.lower():
                    return JSONResponse({"error": msg}, status_code=404)
                return JSONResponse({"error": msg}, status_code=409)

            self.cancel_timeout(incident_id)

        return JSONResponse({
            "incident_id": ctx.id,
            "state": ctx.state.value,
            "action": ctx.remediation.proposed_action if ctx.remediation else None,
            "target": ctx.remediation.target if ctx.remediation else None,
            "approved_by": approved_by,
            "execution_result": ctx.remediation.execution_result if ctx.remediation else None,
            "verdict_id": ctx.verdict_chain[-1] if ctx.verdict_chain else None,
        })

    async def handle_reject(self, request: Request) -> JSONResponse:
        """POST /api/v1/incidents/{id}/reject"""
        incident_id = request.path_params["incident_id"]
        try:
            body = await request.json() if await request.body() else {}
        except (json.JSONDecodeError, ValueError):
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        reason = body.get("reason")
        rejected_by = body.get("rejected_by")

        if not reason:
            return JSONResponse(
                {"error": "reason is required"}, status_code=400
            )

        async with self._get_lock(incident_id):
            try:
                ctx = await self._coordinator.reject(
                    incident_id, reason, rejected_by=rejected_by
                )
            except ValueError as exc:
                msg = str(exc)
                if "not found" in msg.lower():
                    return JSONResponse({"error": msg}, status_code=404)
                return JSONResponse({"error": msg}, status_code=409)

            self.cancel_timeout(incident_id)

        return JSONResponse({
            "incident_id": ctx.id,
            "state": ctx.state.value,
            "rejected_by": rejected_by,
            "reason": reason,
        })

    async def handle_status(self, request: Request) -> JSONResponse:
        """GET /api/v1/incidents/{id}"""
        incident_id = request.path_params["incident_id"]
        ctx = self._context_store.load(incident_id)

        if ctx is None:
            return JSONResponse(
                {"error": f"Incident {incident_id!r} not found"}, status_code=404
            )

        result: dict[str, Any] = {
            "incident_id": ctx.id,
            "state": ctx.state.value,
            "created_at": ctx.created_at,
            "updated_at": ctx.updated_at,
            "trigger_source": ctx.trigger_source,
        }
        if ctx.remediation:
            result["proposed_action"] = ctx.remediation.proposed_action
            result["target"] = ctx.remediation.target
            result["requires_human_approval"] = ctx.remediation.requires_human_approval
            result["executed"] = ctx.remediation.executed
        if ctx.triage:
            result["severity"] = ctx.triage.severity
        return JSONResponse(result)

    async def handle_slack_interaction(self, request: Request) -> Response:
        """POST /api/v1/slack/interactions — Slack callback endpoint."""
        signing_secret = self._config.slack_signing_secret
        if not signing_secret:
            return Response(
                content=json.dumps({"error": "Slack signing secret not configured"}),
                status_code=403,
                media_type="application/json",
            )

        body = await request.body()
        timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
        signature = request.headers.get("X-Slack-Signature", "")

        if not SlackWebClient.verify_signature(signing_secret, timestamp, body, signature):
            return Response(status_code=401)

        # Slack sends payload as form-encoded
        form = await request.form()
        payload_str = form.get("payload", "")
        try:
            payload = json.loads(payload_str)
        except (json.JSONDecodeError, TypeError):
            return Response(status_code=400)

        actions = payload.get("actions", [])
        if not actions:
            return Response(status_code=200)

        action = actions[0]
        action_id = action.get("action_id")
        incident_id = action.get("value")
        if not incident_id:
            return Response(status_code=200)

        user = payload.get("user") or {}
        user_name = user.get("name", "unknown") if isinstance(user, dict) else "unknown"
        channel = payload.get("channel") or {}
        channel_id = channel.get("id") if isinstance(channel, dict) else None
        message = payload.get("message") or {}
        message_ts = message.get("ts") if isinstance(message, dict) else None

        async with self._get_lock(incident_id):
            try:
                if action_id == "approve":
                    ctx = await self._coordinator.approve(
                        incident_id, approved_by=user_name
                    )
                elif action_id == "reject":
                    ctx = await self._coordinator.reject(
                        incident_id,
                        f"Rejected via Slack by {user_name}",
                        rejected_by=user_name,
                    )
                else:
                    return Response(status_code=200)
            except ValueError as exc:
                logger.warning("Slack interaction failed: %s", exc)
                return Response(status_code=200)  # Slack expects 200

            self.cancel_timeout(incident_id)

        # Update original message to remove buttons (fire-and-forget)
        if self._config.slack_bot_token and channel_id and message_ts:
            asyncio.create_task(
                self._update_slack_message(
                    channel_id, message_ts, action_id, user_name, ctx
                )
            )

        return Response(status_code=200)

    async def handle_metrics(self, request: Request) -> Response:
        """GET /metrics — Prometheus text exposition."""
        if self._metrics is None:
            return Response(content="", media_type="text/plain")
        text = self._metrics.collect()
        return Response(
            content=text,
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    async def _update_slack_message(
        self,
        channel_id: str,
        message_ts: str,
        action_id: str,
        user_name: str,
        ctx: Any,
    ) -> None:
        """Replace buttons with confirmation text in the original Slack message."""
        client = SlackWebClient(self._config.slack_bot_token)

        if action_id == "approve":
            status_text = f"\u2705 Approved by @{user_name}"
        else:
            status_text = f"\u274c Rejected by @{user_name}"

        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*{status_text}*"}},
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": f"State: {ctx.state.value} \u00b7 nthlayer-respond"},
            ]},
        ]

        try:
            await client.update_message(channel_id, message_ts, blocks, status_text)
        except Exception as exc:
            logger.warning("Slack message update failed: %s", exc)

    def start_timeout(self, incident_id: str) -> None:
        """Start a background timeout task for an incident."""
        self.cancel_timeout(incident_id)
        task = asyncio.create_task(self._timeout_task(incident_id))
        self._timeouts[incident_id] = task

    def cancel_timeout(self, incident_id: str) -> None:
        """Cancel the timeout task for an incident if active."""
        task = self._timeouts.pop(incident_id, None)
        if task and not task.done():
            task.cancel()
        self._locks.pop(incident_id, None)

    async def _timeout_task(self, incident_id: str, delay: float | None = None) -> None:
        """Wait for delay seconds, then auto-reject if still awaiting approval.

        If delay is None, uses approval_timeout_seconds from config.
        """
        wait = delay if delay is not None else self._config.approval_timeout_seconds
        try:
            await asyncio.sleep(wait)
        except asyncio.CancelledError:
            return

        async with self._get_lock(incident_id):
            ctx = self._context_store.load(incident_id)
            if ctx is None or ctx.state != IncidentState.AWAITING_APPROVAL:
                return

            try:
                await self._coordinator.reject(
                    incident_id,
                    f"Approval timed out after {self._config.approval_timeout_seconds}s",
                    rejected_by="system/timeout",
                )
                logger.info("Approval timed out", extra={"incident_id": incident_id})
            except Exception as exc:
                logger.warning("Timeout reject failed: %s", exc)
            finally:
                self._timeouts.pop(incident_id, None)

    async def recover_pending_approvals(self) -> None:
        """On startup, scan for AWAITING_APPROVAL incidents and start timeouts."""
        active = self._context_store.list_active()
        for incident_id in active:
            ctx = self._context_store.load(incident_id)
            if ctx is None or ctx.state != IncidentState.AWAITING_APPROVAL:
                continue

            try:
                updated = datetime.fromisoformat(ctx.updated_at)
                elapsed = _time.time() - updated.replace(tzinfo=timezone.utc).timestamp()
                remaining = self._config.approval_timeout_seconds - elapsed
            except (ValueError, TypeError):
                remaining = self._config.approval_timeout_seconds

            if remaining <= 0:
                try:
                    await self._coordinator.reject(
                        incident_id,
                        "Approval timed out (expired during server downtime)",
                        rejected_by="system/timeout",
                    )
                except Exception as exc:
                    logger.warning("Timeout recovery reject failed: %s", exc)
            else:
                self.cancel_timeout(incident_id)
                task = asyncio.create_task(self._timeout_task(incident_id, delay=remaining))
                self._timeouts[incident_id] = task
