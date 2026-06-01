"""ntfy notification backend — DND-override push notifications."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import structlog

from nthlayer_workers.respond.notification_backends.protocol import (
    NotificationPayload,
    NotificationResult,
)
from nthlayer_workers.respond.oncall.schedule import RosterMember

logger = structlog.get_logger(__name__)

# Map incident severity to ntfy priority.
# P1 = max (overrides DND), P2 = urgent, P3 = high, P4 = default.
# Unknown severities default to "high" / "warning" (conservative).
PRIORITY_MAP = {1: "max", 2: "urgent", 3: "high", 4: "default"}
_DEFAULT_PRIORITY = "high"

# ntfy tags (emoji) by severity
TAGS_MAP = {
    1: "rotating_light,fire",
    2: "rotating_light,warning",
    3: "warning",
    4: "information_source",
}
_DEFAULT_TAGS = "warning"


class NtfyNotificationBackend:
    """ntfy push notification delivery.

    Sends high-priority notifications that override Do Not Disturb.
    Each roster member has a personal ntfy topic. The ntfy server
    can be self-hosted or use ntfy.sh.
    """

    def __init__(
        self,
        server_url: str | None = None,
        client: Any | None = None,
        webhook_base_url: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.server_url = (
            server_url or os.environ.get("NTFY_SERVER_URL", "https://ntfy.sh")
        )
        self._webhook_base_url = webhook_base_url or os.environ.get(
            "NTHLAYER_WEBHOOK_URL", "http://localhost:8090"
        )
        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            import httpx

            headers = {}
            auth_token = os.environ.get("NTFY_AUTH_TOKEN")
            if auth_token:
                headers["Authorization"] = f"Bearer {auth_token}"
            self._client = httpx.AsyncClient(headers=headers, timeout=timeout)
            self._owns_client = True

    async def close(self) -> None:
        """Release the httpx client if we own it."""
        if self._owns_client:
            await self._client.aclose()

    async def send(
        self, recipient: RosterMember, payload: NotificationPayload
    ) -> NotificationResult:
        if not recipient.ntfy_topic:  # None or empty string
            return NotificationResult(
                delivered=False,
                channel="ntfy",
                recipient=recipient.name,
                timestamp=datetime.now(UTC),
                message_id=None,
                error="No ntfy_topic configured for this user",
            )

        priority = PRIORITY_MAP.get(payload.severity, _DEFAULT_PRIORITY)
        title = f"{payload.incident_id}: {payload.title}"
        body = payload.summary
        if payload.root_cause:
            body += f"\nRoot cause: {payload.root_cause}"

        headers: dict[str, str] = {
            "Title": title,
            "Priority": priority,
            "Tags": TAGS_MAP.get(payload.severity, _DEFAULT_TAGS),
        }

        if payload.actions_url:
            headers["Click"] = payload.actions_url

        if payload.requires_ack:
            ack_url = (
                f"{self._webhook_base_url}/api/v1/incidents/"
                f"{payload.incident_id}/ack"
            )
            headers["Actions"] = (
                f"http, Acknowledge, {ack_url}, method=POST, clear=true"
            )

        try:
            response = await self._client.post(
                f"{self.server_url}/{recipient.ntfy_topic}",
                content=body,
                headers=headers,
            )
            response.raise_for_status()
            try:
                data = response.json()
            except Exception:
                data = {}

            logger.debug(
                "ntfy_sent",
                recipient=recipient.name,
                topic=recipient.ntfy_topic,
                priority=priority,
            )
            return NotificationResult(
                delivered=True,
                channel="ntfy",
                recipient=recipient.name,
                timestamp=datetime.now(UTC),
                message_id=data.get("id"),
                error=None,
            )
        except Exception as exc:
            logger.warning(
                "ntfy_send_failed",
                recipient=recipient.name,
                error=str(exc),
            )
            return NotificationResult(
                delivered=False,
                channel="ntfy",
                recipient=recipient.name,
                timestamp=datetime.now(UTC),
                message_id=None,
                error=str(exc),
            )

    async def health_check(self) -> bool:
        """Check if the ntfy server is reachable."""
        try:
            response = await self._client.get(f"{self.server_url}/v1/health")
            return response.status_code == 200
        except Exception:
            return False
