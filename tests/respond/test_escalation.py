"""Tests for escalation state machine and runner."""

from datetime import datetime, timedelta, timezone

from nthlayer_workers.respond.oncall.escalation import (
    EscalationState,
    EscalationStatus,
    EscalationStep,
)


def _make_steps() -> list[EscalationStep]:
    """Standard 3-step escalation policy for testing."""
    return [
        EscalationStep(after=timedelta(minutes=0), notify="slack_dm"),
        EscalationStep(after=timedelta(minutes=5), notify="ntfy"),
        EscalationStep(
            after=timedelta(minutes=10),
            notify="slack_dm",
            target="next_oncall",
        ),
    ]


def _now() -> datetime:
    return datetime(2026, 4, 13, 14, 0, 0, tzinfo=timezone.utc)


class TestEscalationState:
    """Test EscalationState data + methods."""

    def test_initial_state_is_active(self):
        state = EscalationState(
            incident_id="INC-001",
            started_at=_now(),
            steps=_make_steps(),
        )
        assert state.status == EscalationStatus.ACTIVE
        assert state.current_step_index == 0
        assert state.acknowledged_by is None

    def test_first_step_due_immediately(self):
        """Step with after=0m is due immediately."""
        state = EscalationState(
            incident_id="INC-001",
            started_at=_now(),
            steps=_make_steps(),
        )
        step = state.next_due_step(_now())
        assert step is not None
        assert step.notify == "slack_dm"
        assert state.current_step_index == 1  # Advanced

    def test_second_step_not_due_before_delay(self):
        """Step with after=5m is not due at t+1m."""
        state = EscalationState(
            incident_id="INC-001",
            started_at=_now(),
            steps=_make_steps(),
        )
        # Consume first step
        state.next_due_step(_now())

        # 1 minute later — second step not yet due
        step = state.next_due_step(_now() + timedelta(minutes=1))
        assert step is None

    def test_second_step_due_after_delay(self):
        """Step with after=5m is due at t+5m."""
        state = EscalationState(
            incident_id="INC-001",
            started_at=_now(),
            steps=_make_steps(),
        )
        state.next_due_step(_now())  # Consume first

        step = state.next_due_step(_now() + timedelta(minutes=5))
        assert step is not None
        assert step.notify == "ntfy"

    def test_acknowledge_stops_escalation(self):
        """Acknowledging sets status to ACKNOWLEDGED."""
        state = EscalationState(
            incident_id="INC-001",
            started_at=_now(),
            steps=_make_steps(),
        )
        ack_time = _now() + timedelta(minutes=2)
        state.acknowledge("Alice", ack_time)

        assert state.status == EscalationStatus.ACKNOWLEDGED
        assert state.acknowledged_by == "Alice"
        assert state.acknowledged_at == ack_time

    def test_no_steps_after_acknowledge(self):
        """No further steps fire after acknowledgment."""
        state = EscalationState(
            incident_id="INC-001",
            started_at=_now(),
            steps=_make_steps(),
        )
        state.next_due_step(_now())  # First step fires
        state.acknowledge("Alice", _now() + timedelta(minutes=1))

        # Even though 5m has passed, no step should fire
        step = state.next_due_step(_now() + timedelta(minutes=10))
        assert step is None

    def test_exhausted_when_all_steps_done(self):
        """Status becomes EXHAUSTED when all steps have fired."""
        state = EscalationState(
            incident_id="INC-001",
            started_at=_now(),
            steps=_make_steps(),
        )
        state.next_due_step(_now())  # Step 0
        state.next_due_step(_now() + timedelta(minutes=5))  # Step 1
        state.next_due_step(_now() + timedelta(minutes=10))  # Step 2

        # All consumed — next call should return None and set EXHAUSTED
        step = state.next_due_step(_now() + timedelta(minutes=15))
        assert step is None
        assert state.status == EscalationStatus.EXHAUSTED

    def test_resolve_sets_status(self):
        state = EscalationState(
            incident_id="INC-001",
            started_at=_now(),
            steps=_make_steps(),
        )
        state.resolve()
        assert state.status == EscalationStatus.RESOLVED

    def test_no_steps_after_resolve(self):
        state = EscalationState(
            incident_id="INC-001",
            started_at=_now(),
            steps=_make_steps(),
        )
        state.resolve()

        step = state.next_due_step(_now() + timedelta(minutes=10))
        assert step is None

    def test_time_until_next_step(self):
        """Reports remaining time until next step fires."""
        state = EscalationState(
            incident_id="INC-001",
            started_at=_now(),
            steps=_make_steps(),
        )
        state.next_due_step(_now())  # Consume first (after=0m)

        # Next step is at t+5m, current time is t+2m → 3m remaining
        remaining = state.time_until_next_step(_now() + timedelta(minutes=2))
        assert remaining is not None
        assert remaining == timedelta(minutes=3)

    def test_time_until_next_step_none_when_acked(self):
        state = EscalationState(
            incident_id="INC-001",
            started_at=_now(),
            steps=_make_steps(),
        )
        state.acknowledge("Alice", _now())
        assert state.time_until_next_step(_now()) is None

    def test_time_until_next_step_none_when_exhausted(self):
        state = EscalationState(
            incident_id="INC-001",
            started_at=_now(),
            steps=[EscalationStep(after=timedelta(0), notify="slack_dm")],
        )
        state.next_due_step(_now())  # Consume only step
        state.next_due_step(_now())  # Triggers exhaustion
        assert state.time_until_next_step(_now()) is None

    def test_step_with_target(self):
        """EscalationStep can have a target and phone."""
        step = EscalationStep(
            after=timedelta(minutes=30),
            notify="phone",
            target="engineering_manager",
            phone="+353859876543",
        )
        assert step.target == "engineering_manager"
        assert step.phone == "+353859876543"

    def test_empty_steps_immediately_exhausted(self):
        """Empty steps list immediately exhausts."""
        state = EscalationState(
            incident_id="INC-001",
            started_at=_now(),
            steps=[],
        )
        step = state.next_due_step(_now())
        assert step is None
        assert state.status == EscalationStatus.EXHAUSTED

    def test_notifications_sent_starts_empty(self):
        """notifications_sent list starts empty."""
        state = EscalationState(
            incident_id="INC-001",
            started_at=_now(),
            steps=_make_steps(),
        )
        assert state.notifications_sent == []
