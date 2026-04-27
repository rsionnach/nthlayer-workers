"""Slack notification backend — DM and channel notifications with Block Kit."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog

from nthlayer_workers.respond.notification_backends.protocol import (
    NotificationPayload,
    NotificationResult,
)
from nthlayer_workers.respond.oncall.schedule import RosterMember

logger = structlog.get_logger(__name__)

# red_circle, orange_circle, yellow_circle, blue_circle
SEVERITY_EMOJI = {1: "\U0001f534", 2: "\U0001f7e0", 3: "\U0001f7e1", 4: "\U0001f535"}


class SlackNotificationBackend:
    """Slack notification delivery via Web API.

    Two modes:
    - ``send()``: DM to a specific user via their ``slack_id``
    - ``send_to_channel()``: post to a Slack channel with @here

    Messages include interactive Acknowledge/Escalate buttons via
    Slack Block Kit when ``payload.requires_ack`` is True.
    """

    def __init__(self, client: Any) -> None:  # Any = SlackWebClient or compatible
        self._client = client

    async def send(
        self, recipient: RosterMember, payload: NotificationPayload
    ) -> NotificationResult:
        """Send a DM to the recipient."""
        blocks = _build_incident_blocks(payload)
        fallback = f"{SEVERITY_EMOJI.get(payload.severity, '')} {payload.title}"

        try:
            message_ts = await self._client.post_message(
                channel=recipient.slack_id,
                blocks=blocks,
                text=fallback,
            )
            logger.debug(
                "slack_dm_sent",
                recipient=recipient.name,
                incident_id=payload.incident_id,
            )
            return NotificationResult(
                delivered=True,
                channel="slack_dm",
                recipient=recipient.name,
                timestamp=datetime.now(timezone.utc),
                message_id=message_ts,
                error=None,
            )
        except Exception as exc:
            logger.warning(
                "slack_dm_failed",
                recipient=recipient.name,
                error=str(exc),
            )
            return NotificationResult(
                delivered=False,
                channel="slack_dm",
                recipient=recipient.name,
                timestamp=datetime.now(timezone.utc),
                message_id=None,
                error=str(exc),
            )

    async def send_to_channel(
        self, channel: str, payload: NotificationPayload
    ) -> NotificationResult:
        """Post to a Slack channel with @here."""
        blocks = _build_incident_blocks(payload, include_at_here=True)
        fallback = f"<!here> {SEVERITY_EMOJI.get(payload.severity, '')} {payload.title}"

        try:
            message_ts = await self._client.post_message(
                channel=channel,
                blocks=blocks,
                text=fallback,
            )
            logger.debug(
                "slack_channel_sent",
                channel=channel,
                incident_id=payload.incident_id,
            )
            return NotificationResult(
                delivered=True,
                channel="slack_channel",
                recipient=channel,
                timestamp=datetime.now(timezone.utc),
                message_id=message_ts,
                error=None,
            )
        except Exception as exc:
            logger.warning(
                "slack_channel_failed",
                channel=channel,
                error=str(exc),
            )
            return NotificationResult(
                delivered=False,
                channel="slack_channel",
                recipient=channel,
                timestamp=datetime.now(timezone.utc),
                message_id=None,
                error=str(exc),
            )

    async def health_check(self) -> bool:
        """Check if the Slack client is usable."""
        return self._client is not None and bool(
            getattr(self._client, "bot_token", True)
        )


def _build_incident_blocks(
    payload: NotificationPayload, *, include_at_here: bool = False
) -> list[dict[str, Any]]:
    """Build Slack Block Kit blocks for incident notification."""
    emoji = SEVERITY_EMOJI.get(payload.severity, "")

    header_text = f"{emoji} {payload.incident_id}: {payload.title}"[:150]

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": header_text,
            },
        },
    ]

    if include_at_here:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "<!here>"},
            }
        )

    blocks.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": payload.summary,
            },
        }
    )

    if payload.root_cause:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Root cause:* {payload.root_cause}",
                },
            }
        )

    if payload.blast_radius:
        services = ", ".join(f"`{s}`" for s in payload.blast_radius)
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Blast radius:* {services}",
                },
            }
        )

    if payload.requires_ack:
        buttons: list[dict[str, Any]] = [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Acknowledge"},
                "style": "primary",
                "action_id": "incident_ack",
                "value": payload.incident_id,
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Escalate"},
                "style": "danger",
                "action_id": "incident_escalate",
                "value": payload.incident_id,
            },
        ]
        blocks.append({"type": "actions", "elements": buttons})

    return blocks
