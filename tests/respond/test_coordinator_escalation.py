"""Tests for coordinator escalation integration.

Verifies that the coordinator fires the escalation runner as a
background task after triage (step 0) completes.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nthlayer_workers.respond.coordinator import Coordinator
from nthlayer_workers.respond.oncall.escalation import EscalationState
from nthlayer_workers.respond.oncall.runner import EscalationRunner
from nthlayer_workers.respond.types import (
    AgentRole,
    IncidentContext,
    IncidentState,
)


def _make_context(**overrides) -> IncidentContext:
    """Build a minimal IncidentContext for testing."""
    defaults = {
        "id": "INC-TEST-001",
        "state": IncidentState.TRIGGERED,
        "created_at": "2026-04-13T14:00:00Z",
        "updated_at": "2026-04-13T14:00:00Z",
        "trigger_source": "nthlayer-correlate",
        "trigger_verdict_ids": ["corr-v1"],
        "topology": {},
        "verdict_chain": [],
        "metadata": {
            "service_context": {
                "spec": {
                    "ownership": {
                        "team": "ml-platform",
                        "slack": "#ml-oncall",
                        "oncall": {
                            "timezone": "UTC",
                            "rotation": {
                                "type": "weekly",
                                "handoff": "monday 09:00",
                                "roster": [
                                    {"name": "Alice", "slack_id": "U01"},
                                    {"name": "Bob", "slack_id": "U02"},
                                ],
                            },
                            "escalation": [
                                {"after": "0m", "notify": "slack_dm"},
                                {"after": "5m", "notify": "ntfy"},
                            ],
                        },
                    },
                },
            },
        },
    }
    defaults.update(overrides)
    return IncidentContext(**defaults)


def _make_agents():
    """Build mock agents for all pipeline roles."""
    agents = {}
    for role in AgentRole:
        mock = AsyncMock()
        mock.execute = AsyncMock()
        agents[role] = mock
    return agents


def _make_config():
    """Build a mock config."""
    config = MagicMock()
    config.escalation_threshold = 0.3
    config.approval_timeout_seconds = 900
    return config


class TestCoordinatorEscalation:
    """Test escalation runner integration in coordinator."""

    @pytest.mark.asyncio
    async def test_coordinator_accepts_escalation_runner(self):
        """Coordinator can be constructed with an escalation_runner."""
        agents = _make_agents()
        context_store = MagicMock()
        verdict_store = MagicMock()
        verdict_store.get.return_value = None
        runner = MagicMock(spec=EscalationRunner)

        coordinator = Coordinator(
            agents=agents,
            context_store=context_store,
            verdict_store=verdict_store,
            config=_make_config(),
            escalation_runner=runner,
        )
        assert coordinator._escalation_runner is runner

    @pytest.mark.asyncio
    async def test_coordinator_without_runner_works(self):
        """Coordinator works fine without an escalation_runner (backward compat)."""
        agents = _make_agents()
        context_store = MagicMock()
        context_store.load.return_value = None
        verdict_store = MagicMock()
        verdict_store.get.return_value = None

        coordinator = Coordinator(
            agents=agents,
            context_store=context_store,
            verdict_store=verdict_store,
            config=_make_config(),
        )
        assert coordinator._escalation_runner is None

    @pytest.mark.asyncio
    async def test_escalation_fired_after_triage(self):
        """After triage completes, coordinator fires the escalation runner."""
        agents = _make_agents()
        context_store = MagicMock()
        context_store.save = MagicMock()
        verdict_store = MagicMock()
        verdict_store.get.return_value = None

        mock_runner = AsyncMock(spec=EscalationRunner)
        mock_runner.start_escalation = AsyncMock(
            return_value=EscalationState(
                incident_id="INC-TEST-001",
                started_at=MagicMock(),
                steps=[],
            )
        )

        coordinator = Coordinator(
            agents=agents,
            context_store=context_store,
            verdict_store=verdict_store,
            config=_make_config(),
            escalation_runner=mock_runner,
        )

        context = _make_context()
        await coordinator.run(context)

        # Escalation runner should have been called after triage
        mock_runner.start_escalation.assert_called_once()
        call_kwargs = mock_runner.start_escalation.call_args.kwargs
        assert call_kwargs["incident_id"] == "INC-TEST-001"
        assert len(call_kwargs["steps"]) == 2
        assert call_kwargs["steps"][0].notify == "slack_dm"
        assert call_kwargs["steps"][1].notify == "ntfy"
        assert call_kwargs["payload"].requires_ack is True

    @pytest.mark.asyncio
    async def test_no_escalation_without_oncall_config(self):
        """If no oncall config in manifest, no escalation is started."""
        agents = _make_agents()
        context_store = MagicMock()
        context_store.save = MagicMock()
        verdict_store = MagicMock()
        verdict_store.get.return_value = None

        mock_runner = AsyncMock(spec=EscalationRunner)

        coordinator = Coordinator(
            agents=agents,
            context_store=context_store,
            verdict_store=verdict_store,
            config=_make_config(),
            escalation_runner=mock_runner,
        )

        # Context without oncall config
        context = _make_context(
            metadata={
                "service_context": {
                    "spec": {
                        "ownership": {"team": "test"},
                    },
                },
            },
        )
        await coordinator.run(context)

        mock_runner.start_escalation.assert_not_called()
