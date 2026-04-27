"""Tests for delegation mode — 'I'm busy, handle it'."""

from datetime import timedelta

from nthlayer_workers.respond.sre.delegation import (
    Delegation,
    DelegationStatus,
    check_delegation_expired,
    create_delegation,
    should_notify_delegator,
)


class TestCreateDelegation:
    """Test delegation creation."""

    def test_creates_delegation(self):
        delegation = create_delegation(
            incident_id="INC-001",
            delegated_by="rob",
            safe_actions_only=True,
        )

        assert isinstance(delegation, Delegation)
        assert delegation.incident_id == "INC-001"
        assert delegation.delegated_by == "rob"
        assert delegation.safe_actions_only is True
        assert delegation.status == DelegationStatus.ACTIVE

    def test_default_max_duration(self):
        delegation = create_delegation(
            incident_id="INC-001",
            delegated_by="rob",
        )

        assert delegation.max_duration == timedelta(hours=2)

    def test_custom_max_duration(self):
        delegation = create_delegation(
            incident_id="INC-001",
            delegated_by="rob",
            max_duration=timedelta(hours=4),
        )

        assert delegation.max_duration == timedelta(hours=4)

    def test_expires_at_matches_max_duration(self):
        delegation = create_delegation(
            incident_id="INC-001",
            delegated_by="rob",
        )

        assert delegation.expires_at is not None
        assert delegation.expires_at == delegation.delegated_at + delegation.max_duration


class TestCheckDelegationExpired:
    """Test delegation expiry checking."""

    def test_not_expired_within_window(self):
        delegation = create_delegation(
            incident_id="INC-001",
            delegated_by="rob",
            max_duration=timedelta(hours=2),
        )

        now = delegation.delegated_at + timedelta(minutes=30)
        assert check_delegation_expired(delegation, now) is False

    def test_expired_after_window(self):
        delegation = create_delegation(
            incident_id="INC-001",
            delegated_by="rob",
            max_duration=timedelta(hours=2),
        )

        now = delegation.delegated_at + timedelta(hours=3)
        assert check_delegation_expired(delegation, now) is True

    def test_expired_at_exact_boundary(self):
        """At exactly expires_at, the delegation is expired."""
        delegation = create_delegation(
            incident_id="INC-001",
            delegated_by="rob",
            max_duration=timedelta(hours=2),
        )

        now = delegation.expires_at
        assert check_delegation_expired(delegation, now) is True

    def test_already_resolved_not_expired(self):
        delegation = create_delegation(
            incident_id="INC-001",
            delegated_by="rob",
        )
        delegation.status = DelegationStatus.RESOLVED

        now = delegation.delegated_at + timedelta(hours=5)
        assert check_delegation_expired(delegation, now) is False

    def test_already_escalated_not_expired(self):
        delegation = create_delegation(
            incident_id="INC-001",
            delegated_by="rob",
        )
        delegation.status = DelegationStatus.ESCALATED

        now = delegation.delegated_at + timedelta(hours=5)
        assert check_delegation_expired(delegation, now) is False


class TestShouldNotifyDelegator:
    """Test notification filtering during delegation."""

    def test_resolution_notifies_when_active(self):
        delegation = create_delegation(
            incident_id="INC-001",
            delegated_by="rob",
        )

        assert should_notify_delegator(delegation, "resolution") is True

    def test_escalation_notifies_when_active(self):
        delegation = create_delegation(
            incident_id="INC-001",
            delegated_by="rob",
        )

        assert should_notify_delegator(delegation, "escalation") is True

    def test_status_update_suppressed_when_active(self):
        delegation = create_delegation(
            incident_id="INC-001",
            delegated_by="rob",
        )

        assert should_notify_delegator(delegation, "status_update") is False

    def test_investigation_suppressed_when_active(self):
        delegation = create_delegation(
            incident_id="INC-001",
            delegated_by="rob",
        )

        assert should_notify_delegator(delegation, "investigation") is False

    def test_all_events_pass_through_when_resolved(self):
        """Resolved delegation does not filter notifications."""
        delegation = create_delegation(
            incident_id="INC-001",
            delegated_by="rob",
        )
        delegation.status = DelegationStatus.RESOLVED

        assert should_notify_delegator(delegation, "status_update") is True
        assert should_notify_delegator(delegation, "investigation") is True

    def test_empty_event_type_suppressed(self):
        delegation = create_delegation(
            incident_id="INC-001",
            delegated_by="rob",
        )

        assert should_notify_delegator(delegation, "") is False
