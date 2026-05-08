"""Tests for notification backends (stdout, Slack, ntfy)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nthlayer_workers.respond.notification_backends.ntfy_backend import (
    NtfyNotificationBackend,
)
from nthlayer_workers.respond.notification_backends.protocol import (
    NotificationPayload,
    NotificationResult,
)
from nthlayer_workers.respond.notification_backends.slack_backend import (
    SlackNotificationBackend,
)
from nthlayer_workers.respond.notification_backends.stdout_backend import (
    StdoutNotificationBackend,
)
from nthlayer_workers.respond.oncall.schedule import RosterMember


def _make_payload(**overrides) -> NotificationPayload:
    """Build a minimal NotificationPayload for testing."""
    defaults = {
        "incident_id": "INC-FRAUD-20260413",
        "severity": 2,
        "title": "fraud-detect reversal rate breach",
        "summary": "Reversal rate at 8%, target 1.5%.",
        "root_cause": "Deploy v2.3.1",
        "blast_radius": ["fraud-detect", "payment-api"],
        "actions_url": None,
        "escalation_step": 0,
        "requires_ack": True,
    }
    defaults.update(overrides)
    return NotificationPayload(**defaults)


def _make_recipient(**overrides) -> RosterMember:
    """Build a minimal RosterMember for testing."""
    defaults = {
        "name": "Alice",
        "slack_id": "U0123ALICE",
        "ntfy_topic": "oncall-alice",
        "phone": "+353851234567",
    }
    defaults.update(overrides)
    return RosterMember(**defaults)


# =============================================================================
# Stdout Backend
# =============================================================================


class TestStdoutBackend:
    """Test stdout notification backend."""

    @pytest.mark.asyncio
    async def test_send_returns_delivered(self):
        backend = StdoutNotificationBackend()
        result = await backend.send(_make_recipient(), _make_payload())

        assert isinstance(result, NotificationResult)
        assert result.delivered is True
        assert result.channel == "stdout"
        assert result.recipient == "Alice"
        assert result.error is None

    @pytest.mark.asyncio
    async def test_send_prints_to_stdout(self, capsys):
        backend = StdoutNotificationBackend()
        await backend.send(_make_recipient(), _make_payload())

        captured = capsys.readouterr()
        assert "INC-FRAUD-20260413" in captured.out
        assert "fraud-detect reversal rate breach" in captured.out
        assert "Alice" in captured.out

    @pytest.mark.asyncio
    async def test_health_check_always_true(self):
        backend = StdoutNotificationBackend()
        assert await backend.health_check() is True

    @pytest.mark.asyncio
    async def test_send_with_no_root_cause(self, capsys):
        backend = StdoutNotificationBackend()
        result = await backend.send(
            _make_recipient(), _make_payload(root_cause=None)
        )
        assert result.delivered is True
        captured = capsys.readouterr()
        assert "Root cause" not in captured.out

    @pytest.mark.asyncio
    async def test_send_with_empty_blast_radius(self, capsys):
        backend = StdoutNotificationBackend()
        await backend.send(_make_recipient(), _make_payload(blast_radius=[]))
        captured = capsys.readouterr()
        assert "Blast radius" not in captured.out

    @pytest.mark.asyncio
    async def test_send_with_unknown_severity(self):
        backend = StdoutNotificationBackend()
        result = await backend.send(
            _make_recipient(), _make_payload(severity=99)
        )
        assert result.delivered is True


# =============================================================================
# Slack Backend
# =============================================================================


class TestSlackBackend:
    """Test Slack notification backend."""

    @pytest.mark.asyncio
    async def test_send_dm_calls_post_message(self):
        mock_client = AsyncMock()
        mock_client.post_message.return_value = "1234567890.123456"

        backend = SlackNotificationBackend(client=mock_client)
        result = await backend.send(_make_recipient(), _make_payload())

        assert result.delivered is True
        assert result.channel == "slack_dm"
        assert result.recipient == "Alice"
        assert result.message_id == "1234567890.123456"
        mock_client.post_message.assert_called_once()

        call_kwargs = mock_client.post_message.call_args
        assert call_kwargs.kwargs["channel"] == "U0123ALICE"

    @pytest.mark.asyncio
    async def test_send_includes_block_kit_blocks(self):
        mock_client = AsyncMock()
        mock_client.post_message.return_value = "ts123"

        backend = SlackNotificationBackend(client=mock_client)
        await backend.send(_make_recipient(), _make_payload())

        call_kwargs = mock_client.post_message.call_args.kwargs
        blocks = call_kwargs["blocks"]
        assert isinstance(blocks, list)
        assert len(blocks) >= 2  # At least header + summary

        header_text = blocks[0]["text"]["text"]
        assert "INC-FRAUD-20260413" in header_text

    @pytest.mark.asyncio
    async def test_send_includes_ack_buttons_when_required(self):
        mock_client = AsyncMock()
        mock_client.post_message.return_value = "ts123"

        backend = SlackNotificationBackend(client=mock_client)
        await backend.send(_make_recipient(), _make_payload(requires_ack=True))

        call_kwargs = mock_client.post_message.call_args.kwargs
        blocks = call_kwargs["blocks"]
        action_blocks = [b for b in blocks if b.get("type") == "actions"]
        assert len(action_blocks) == 1

        buttons = action_blocks[0]["elements"]
        action_ids = {b["action_id"] for b in buttons}
        assert "incident_ack" in action_ids
        assert "incident_escalate" in action_ids

    @pytest.mark.asyncio
    async def test_send_no_buttons_when_ack_not_required(self):
        mock_client = AsyncMock()
        mock_client.post_message.return_value = "ts123"

        backend = SlackNotificationBackend(client=mock_client)
        await backend.send(_make_recipient(), _make_payload(requires_ack=False))

        call_kwargs = mock_client.post_message.call_args.kwargs
        blocks = call_kwargs["blocks"]
        action_blocks = [b for b in blocks if b.get("type") == "actions"]
        assert len(action_blocks) == 0

    @pytest.mark.asyncio
    async def test_send_failure_returns_error_result(self):
        mock_client = AsyncMock()
        mock_client.post_message.side_effect = Exception("Slack API error")

        backend = SlackNotificationBackend(client=mock_client)
        result = await backend.send(_make_recipient(), _make_payload())

        assert result.delivered is False
        assert result.channel == "slack_dm"
        assert "Slack API error" in result.error

    @pytest.mark.asyncio
    async def test_send_to_channel(self):
        mock_client = AsyncMock()
        mock_client.post_message.return_value = "ts123"

        backend = SlackNotificationBackend(client=mock_client)
        result = await backend.send_to_channel(
            "#ml-platform-oncall", _make_payload()
        )

        assert result.delivered is True
        assert result.channel == "slack_channel"
        call_kwargs = mock_client.post_message.call_args.kwargs
        assert call_kwargs["channel"] == "#ml-platform-oncall"

    @pytest.mark.asyncio
    async def test_send_to_channel_failure(self):
        mock_client = AsyncMock()
        mock_client.post_message.side_effect = Exception("channel not found")

        backend = SlackNotificationBackend(client=mock_client)
        result = await backend.send_to_channel("#nonexistent", _make_payload())

        assert result.delivered is False
        assert result.channel == "slack_channel"
        assert "channel not found" in result.error

    @pytest.mark.asyncio
    async def test_send_to_channel_includes_at_here_block(self):
        mock_client = AsyncMock()
        mock_client.post_message.return_value = "ts123"

        backend = SlackNotificationBackend(client=mock_client)
        await backend.send_to_channel("#oncall", _make_payload())

        call_kwargs = mock_client.post_message.call_args.kwargs
        blocks = call_kwargs["blocks"]
        at_here_blocks = [
            b for b in blocks
            if b.get("type") == "section"
            and "<!here>" in b.get("text", {}).get("text", "")
        ]
        assert len(at_here_blocks) == 1

    @pytest.mark.asyncio
    async def test_health_check(self):
        mock_client = AsyncMock()
        backend = SlackNotificationBackend(client=mock_client)
        result = await backend.health_check()
        assert isinstance(result, bool)

    @pytest.mark.asyncio
    async def test_send_with_unknown_severity(self):
        mock_client = AsyncMock()
        mock_client.post_message.return_value = "ts123"

        backend = SlackNotificationBackend(client=mock_client)
        result = await backend.send(_make_recipient(), _make_payload(severity=99))
        assert result.delivered is True


# =============================================================================
# ntfy Backend
# =============================================================================


class TestNtfyBackend:
    """Test ntfy notification backend."""

    @pytest.mark.asyncio
    async def test_send_posts_to_ntfy_topic(self):
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {"id": "ntfy-msg-123"}
        mock_response.raise_for_status = MagicMock()
        mock_client.post.return_value = mock_response

        backend = NtfyNotificationBackend(
            server_url="https://ntfy.example.com", client=mock_client
        )
        result = await backend.send(_make_recipient(), _make_payload())

        assert result.delivered is True
        assert result.channel == "ntfy"
        assert result.recipient == "Alice"
        assert result.message_id == "ntfy-msg-123"

        call_args = mock_client.post.call_args
        assert call_args.args[0] == "https://ntfy.example.com/oncall-alice"

    @pytest.mark.asyncio
    async def test_send_maps_severity_to_priority(self):
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {"id": "msg1"}
        mock_response.raise_for_status = MagicMock()
        mock_client.post.return_value = mock_response

        backend = NtfyNotificationBackend(
            server_url="https://ntfy.sh", client=mock_client
        )

        # P1 (severity=1) should map to "max" priority
        await backend.send(_make_recipient(), _make_payload(severity=1))
        headers = mock_client.post.call_args.kwargs.get("headers", {})
        assert headers.get("Priority") == "max"

    @pytest.mark.asyncio
    async def test_send_without_ntfy_topic_returns_error(self):
        mock_client = AsyncMock()
        backend = NtfyNotificationBackend(
            server_url="https://ntfy.sh", client=mock_client
        )
        recipient = _make_recipient(ntfy_topic=None)
        result = await backend.send(recipient, _make_payload())

        assert result.delivered is False
        assert "topic" in result.error.lower()
        mock_client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_with_empty_ntfy_topic_returns_error(self):
        mock_client = AsyncMock()
        backend = NtfyNotificationBackend(
            server_url="https://ntfy.sh", client=mock_client
        )
        recipient = _make_recipient(ntfy_topic="")
        result = await backend.send(recipient, _make_payload())

        assert result.delivered is False
        assert "topic" in result.error.lower()
        mock_client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_failure_returns_error_result(self):
        mock_client = AsyncMock()
        mock_client.post.side_effect = Exception("Connection refused")

        backend = NtfyNotificationBackend(
            server_url="https://ntfy.sh", client=mock_client
        )
        result = await backend.send(_make_recipient(), _make_payload())

        assert result.delivered is False
        assert "Connection refused" in result.error

    @pytest.mark.asyncio
    async def test_health_check_queries_server(self):
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.get.return_value = mock_response

        backend = NtfyNotificationBackend(
            server_url="https://ntfy.sh", client=mock_client
        )
        assert await backend.health_check() is True

    @pytest.mark.asyncio
    async def test_health_check_returns_false_on_error(self):
        mock_client = AsyncMock()
        mock_client.get.side_effect = Exception("unreachable")

        backend = NtfyNotificationBackend(
            server_url="https://ntfy.sh", client=mock_client
        )
        assert await backend.health_check() is False

    @pytest.mark.asyncio
    async def test_send_includes_ack_action_when_required(self):
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {"id": "msg1"}
        mock_response.raise_for_status = MagicMock()
        mock_client.post.return_value = mock_response

        backend = NtfyNotificationBackend(
            server_url="https://ntfy.sh",
            client=mock_client,
            webhook_base_url="https://nthlayer.example.com",
        )
        await backend.send(_make_recipient(), _make_payload(requires_ack=True))

        headers = mock_client.post.call_args.kwargs.get("headers", {})
        assert "Actions" in headers
        assert "Acknowledge" in headers["Actions"]

    @pytest.mark.asyncio
    async def test_send_with_unknown_severity(self):
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {"id": "msg1"}
        mock_response.raise_for_status = MagicMock()
        mock_client.post.return_value = mock_response

        backend = NtfyNotificationBackend(
            server_url="https://ntfy.sh", client=mock_client
        )
        result = await backend.send(
            _make_recipient(), _make_payload(severity=99)
        )
        assert result.delivered is True

        headers = mock_client.post.call_args.kwargs.get("headers", {})
        assert headers.get("Priority") == "high"  # default fallback
        assert headers.get("Tags") == "warning"  # default fallback


# -- Slack threading (opensrm-st4s.4) --

class TestSlackThreading:
    """Repeat sends for the same incident thread under the original message."""

    @pytest.mark.asyncio
    async def test_first_send_no_thread_ts(self):
        mock_client = AsyncMock()
        mock_client.post_message.return_value = "ts-1"
        backend = SlackNotificationBackend(client=mock_client)
        await backend.send(_make_recipient(), _make_payload())
        kwargs = mock_client.post_message.call_args.kwargs
        assert kwargs.get("thread_ts") is None

    @pytest.mark.asyncio
    async def test_second_send_threads_under_first(self):
        mock_client = AsyncMock()
        mock_client.post_message.side_effect = ["ts-anchor", "ts-reply"]
        backend = SlackNotificationBackend(client=mock_client)
        recipient = _make_recipient()
        payload = _make_payload()

        first = await backend.send(recipient, payload)
        second = await backend.send(recipient, payload)

        assert first.message_id == "ts-anchor"
        assert second.message_id == "ts-reply"
        # Second call must thread under the first.
        second_kwargs = mock_client.post_message.call_args_list[1].kwargs
        assert second_kwargs["thread_ts"] == "ts-anchor"

    @pytest.mark.asyncio
    async def test_different_incidents_independent_threads(self):
        mock_client = AsyncMock()
        mock_client.post_message.side_effect = ["ts-A", "ts-B", "ts-A2"]
        backend = SlackNotificationBackend(client=mock_client)
        recipient = _make_recipient()

        await backend.send(recipient, _make_payload(incident_id="INC-A"))
        await backend.send(recipient, _make_payload(incident_id="INC-B"))
        await backend.send(recipient, _make_payload(incident_id="INC-A"))

        kwargs_list = mock_client.post_message.call_args_list
        # First send for INC-A: no thread.
        assert kwargs_list[0].kwargs.get("thread_ts") is None
        # First send for INC-B: no thread (separate incident).
        assert kwargs_list[1].kwargs.get("thread_ts") is None
        # Second send for INC-A: threads under ts-A, NOT ts-B.
        assert kwargs_list[2].kwargs["thread_ts"] == "ts-A"

    @pytest.mark.asyncio
    async def test_channel_post_threads_repeat_sends(self):
        mock_client = AsyncMock()
        mock_client.post_message.side_effect = ["ch-anchor", "ch-reply"]
        backend = SlackNotificationBackend(client=mock_client)
        payload = _make_payload()

        await backend.send_to_channel("#oncall", payload)
        await backend.send_to_channel("#oncall", payload)

        kwargs_list = mock_client.post_message.call_args_list
        assert kwargs_list[0].kwargs.get("thread_ts") is None
        assert kwargs_list[1].kwargs["thread_ts"] == "ch-anchor"

    @pytest.mark.asyncio
    async def test_channel_thread_reply_omits_at_here(self):
        """Thread replies must not re-page the channel via @here."""
        mock_client = AsyncMock()
        mock_client.post_message.side_effect = ["ch-anchor", "ch-reply"]
        backend = SlackNotificationBackend(client=mock_client)
        payload = _make_payload()

        await backend.send_to_channel("#oncall", payload)
        await backend.send_to_channel("#oncall", payload)

        first_text = mock_client.post_message.call_args_list[0].kwargs["text"]
        reply_text = mock_client.post_message.call_args_list[1].kwargs["text"]
        assert "<!here>" in first_text
        assert "<!here>" not in reply_text

    @pytest.mark.asyncio
    async def test_dm_and_channel_threads_tracked_independently(self):
        """A DM thread and a channel thread for the same incident are separate."""
        mock_client = AsyncMock()
        mock_client.post_message.side_effect = ["dm-anchor", "ch-anchor"]
        backend = SlackNotificationBackend(client=mock_client)
        payload = _make_payload(incident_id="INC-X")

        await backend.send(_make_recipient(), payload)
        await backend.send_to_channel("#oncall", payload)

        # Both posts started fresh threads; neither piggybacks on the other.
        kwargs_list = mock_client.post_message.call_args_list
        assert kwargs_list[0].kwargs.get("thread_ts") is None
        assert kwargs_list[1].kwargs.get("thread_ts") is None

    @pytest.mark.asyncio
    async def test_failed_first_send_does_not_anchor_thread(self):
        """If the first send fails, the next send tries a fresh top-level message."""
        mock_client = AsyncMock()
        mock_client.post_message.side_effect = [Exception("rate limit"), "ts-recovered"]
        backend = SlackNotificationBackend(client=mock_client)
        recipient = _make_recipient()
        payload = _make_payload()

        first = await backend.send(recipient, payload)
        second = await backend.send(recipient, payload)

        assert first.delivered is False
        assert second.delivered is True
        # Second call: no thread_ts (the failed first never anchored).
        second_kwargs = mock_client.post_message.call_args_list[1].kwargs
        assert second_kwargs.get("thread_ts") is None
