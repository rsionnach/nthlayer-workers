"""Tests for escalation runner — async loop driving the state machine."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from nthlayer_workers.respond.notification_backends.protocol import (
    NotificationPayload,
    NotificationResult,
)
from nthlayer_workers.respond.oncall.escalation import (
    EscalationState,
    EscalationStatus,
    EscalationStep,
)
from nthlayer_workers.respond.oncall.runner import EscalationRunner


def _make_payload(**overrides) -> NotificationPayload:
    defaults = {
        "incident_id": "INC-FRAUD-001",
        "severity": 2,
        "title": "fraud-detect breach",
        "summary": "Reversal rate at 8%.",
        "root_cause": None,
        "blast_radius": ["fraud-detect"],
        "actions_url": None,
        "escalation_step": 0,
        "requires_ack": True,
    }
    defaults.update(overrides)
    return NotificationPayload(**defaults)


def _make_oncall_config() -> dict:
    return {
        "timezone": "UTC",
        "rotation": {
            "type": "weekly",
            "handoff": "monday 09:00",
            "roster": [
                {"name": "Alice", "slack_id": "U01", "ntfy_topic": "oncall-alice"},
                {"name": "Bob", "slack_id": "U02", "ntfy_topic": "oncall-bob"},
            ],
        },
    }


def _make_steps() -> list[EscalationStep]:
    return [
        EscalationStep(after=timedelta(0), notify="slack_dm"),
        EscalationStep(after=timedelta(minutes=5), notify="ntfy"),
        EscalationStep(
            after=timedelta(minutes=10),
            notify="slack_dm",
            target="next_oncall",
        ),
    ]


class TestEscalationRunner:
    """Test the escalation runner."""

    def test_runner_init(self):
        """Runner stores backends and oncall config."""
        mock_backend = AsyncMock()
        runner = EscalationRunner(
            backends={"slack_dm": mock_backend},
            oncall_config=_make_oncall_config(),
        )
        assert "slack_dm" in runner.backends
        assert runner._active_escalations == {}

    @pytest.mark.asyncio
    async def test_start_escalation_creates_state(self):
        """Starting escalation creates an EscalationState and stores it."""
        mock_backend = AsyncMock()
        mock_backend.send.return_value = NotificationResult(
            delivered=True,
            channel="slack_dm",
            recipient="Alice",
            timestamp=datetime.now(timezone.utc),
            message_id="ts1",
            error=None,
        )
        runner = EscalationRunner(
            backends={"slack_dm": mock_backend},
            oncall_config=_make_oncall_config(),
        )

        state = await runner.start_escalation(
            incident_id="INC-001",
            payload=_make_payload(),
            steps=_make_steps(),
        )

        assert isinstance(state, EscalationState)
        assert state.incident_id == "INC-001"
        assert "INC-001" in runner._active_escalations

    @pytest.mark.asyncio
    async def test_execute_step_sends_to_correct_backend(self):
        """Executing a step dispatches to the right notification backend."""
        mock_slack = AsyncMock()
        mock_slack.send.return_value = NotificationResult(
            delivered=True,
            channel="slack_dm",
            recipient="Alice",
            timestamp=datetime.now(timezone.utc),
            message_id="ts1",
            error=None,
        )
        runner = EscalationRunner(
            backends={"slack_dm": mock_slack},
            oncall_config=_make_oncall_config(),
        )

        state = EscalationState(
            incident_id="INC-001",
            started_at=datetime.now(timezone.utc),
            steps=_make_steps(),
        )
        step = _make_steps()[0]  # slack_dm, no target

        await runner._execute_step(state, step, _make_payload())

        mock_slack.send.assert_called_once()
        assert len(state.notifications_sent) == 1
        assert state.notifications_sent[0].delivered is True

    @pytest.mark.asyncio
    async def test_execute_step_next_oncall_target(self):
        """Step with target='next_oncall' sends to secondary, not primary."""
        mock_slack = AsyncMock()
        mock_slack.send.return_value = NotificationResult(
            delivered=True,
            channel="slack_dm",
            recipient="secondary",
            timestamp=datetime.now(timezone.utc),
            message_id="ts2",
            error=None,
        )
        runner = EscalationRunner(
            backends={"slack_dm": mock_slack},
            oncall_config=_make_oncall_config(),
        )

        state = EscalationState(
            incident_id="INC-001",
            started_at=datetime.now(timezone.utc),
            steps=_make_steps(),
        )

        # First send a step without target to capture primary
        step_primary = EscalationStep(after=timedelta(0), notify="slack_dm")
        await runner._execute_step(state, step_primary, _make_payload())
        primary_name = mock_slack.send.call_args.args[0].name

        mock_slack.reset_mock()

        # Now send step with target=next_oncall — should be different person
        step_secondary = EscalationStep(
            after=timedelta(minutes=10),
            notify="slack_dm",
            target="next_oncall",
        )
        await runner._execute_step(state, step_secondary, _make_payload())

        secondary_name = mock_slack.send.call_args.args[0].name
        assert secondary_name != primary_name

    @pytest.mark.asyncio
    async def test_acknowledge_stops_active_escalation(self):
        """Acknowledging an incident marks escalation as acknowledged."""
        mock_backend = AsyncMock()
        mock_backend.send.return_value = NotificationResult(
            delivered=True,
            channel="slack_dm",
            recipient="Alice",
            timestamp=datetime.now(timezone.utc),
            message_id="ts1",
            error=None,
        )
        runner = EscalationRunner(
            backends={"slack_dm": mock_backend},
            oncall_config=_make_oncall_config(),
        )

        state = await runner.start_escalation(
            incident_id="INC-001",
            payload=_make_payload(),
            steps=_make_steps(),
        )

        await runner.acknowledge("INC-001", "Alice")

        assert state.status == EscalationStatus.ACKNOWLEDGED
        assert state.acknowledged_by == "Alice"

    @pytest.mark.asyncio
    async def test_acknowledge_unknown_incident_is_noop(self):
        """Acknowledging a non-existent incident does not raise."""
        runner = EscalationRunner(
            backends={},
            oncall_config=_make_oncall_config(),
        )
        await runner.acknowledge("INC-NONEXISTENT", "Alice")
        # Should not raise

    @pytest.mark.asyncio
    async def test_missing_backend_logs_warning(self):
        """Step targeting a missing backend logs a warning, doesn't crash."""
        runner = EscalationRunner(
            backends={},  # No backends configured
            oncall_config=_make_oncall_config(),
        )

        state = EscalationState(
            incident_id="INC-001",
            started_at=datetime.now(timezone.utc),
            steps=[EscalationStep(after=timedelta(0), notify="ntfy")],
        )

        # Should not raise even though "ntfy" backend is missing
        await runner._execute_step(
            state,
            EscalationStep(after=timedelta(0), notify="ntfy"),
            _make_payload(),
        )
        assert len(state.notifications_sent) == 0

    @pytest.mark.asyncio
    async def test_execute_step_with_slack_channel(self):
        """Step with notify='slack_channel' uses send_to_channel."""
        mock_slack = AsyncMock()
        mock_slack.send_to_channel.return_value = NotificationResult(
            delivered=True,
            channel="slack_channel",
            recipient="#oncall",
            timestamp=datetime.now(timezone.utc),
            message_id="ts3",
            error=None,
        )
        runner = EscalationRunner(
            backends={"slack_dm": mock_slack},
            oncall_config=_make_oncall_config(),
            slack_channel="#oncall",
        )

        state = EscalationState(
            incident_id="INC-001",
            started_at=datetime.now(timezone.utc),
            steps=[],
        )
        step = EscalationStep(after=timedelta(0), notify="slack_channel")

        await runner._execute_step(state, step, _make_payload())

        mock_slack.send_to_channel.assert_called_once()
