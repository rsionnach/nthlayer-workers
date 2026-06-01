"""Tests for notification backend protocol and dataclasses."""

from datetime import UTC, datetime

from nthlayer_workers.respond.notification_backends.protocol import (
    NotificationBackend,
    NotificationPayload,
    NotificationResult,
)


class TestNotificationPayload:
    """Test NotificationPayload dataclass."""

    def test_full_payload(self):
        payload = NotificationPayload(
            incident_id="INC-FRAUD-20260413",
            severity=2,
            title="fraud-detect reversal rate breach",
            summary="Reversal rate at 8%, target 1.5%. Affecting payment flow.",
            root_cause="Deploy v2.3.1 modified connection pooling config",
            blast_radius=["fraud-detect", "payment-api"],
            actions_url="https://dashboard.example.com/incidents/INC-FRAUD-20260413",
            escalation_step=0,
            requires_ack=True,
        )
        assert payload.incident_id == "INC-FRAUD-20260413"
        assert payload.severity == 2
        assert payload.blast_radius == ["fraud-detect", "payment-api"]
        assert payload.requires_ack is True

    def test_payload_optional_fields(self):
        payload = NotificationPayload(
            incident_id="INC-TEST-001",
            severity=3,
            title="test alert",
            summary="test summary",
            root_cause=None,
            blast_radius=[],
            actions_url=None,
            escalation_step=0,
            requires_ack=False,
        )
        assert payload.root_cause is None
        assert payload.actions_url is None
        assert payload.blast_radius == []


class TestNotificationResult:
    """Test NotificationResult dataclass."""

    def test_successful_result(self):
        now = datetime.now(UTC)
        result = NotificationResult(
            delivered=True,
            channel="slack_dm",
            recipient="Alice",
            timestamp=now,
            message_id="1234567890.123456",
            error=None,
        )
        assert result.delivered is True
        assert result.channel == "slack_dm"
        assert result.message_id == "1234567890.123456"
        assert result.error is None

    def test_failed_result(self):
        now = datetime.now(UTC)
        result = NotificationResult(
            delivered=False,
            channel="ntfy",
            recipient="Bob",
            timestamp=now,
            message_id=None,
            error="Connection refused",
        )
        assert result.delivered is False
        assert result.error == "Connection refused"


class TestNotificationBackendProtocol:
    """Test NotificationBackend protocol conformance."""

    def test_protocol_is_runtime_checkable(self):
        """NotificationBackend can be checked with isinstance."""
        assert hasattr(NotificationBackend, "__protocol_attrs__") or hasattr(
            NotificationBackend, "_is_runtime_protocol"
        )

    def test_concrete_stub_satisfies_protocol(self):
        """A minimal concrete class satisfies the protocol at runtime."""

        class StubBackend:
            async def send(self, recipient, payload):
                return NotificationResult(
                    delivered=True,
                    channel="stub",
                    recipient=recipient.name,
                    timestamp=datetime.now(UTC),
                    message_id=None,
                    error=None,
                )

            async def health_check(self):
                return True

        stub = StubBackend()
        assert isinstance(stub, NotificationBackend)

    def test_non_conforming_class_rejected(self):
        """A class missing required methods does not satisfy the protocol."""

        class Incomplete:
            async def send(self, recipient, payload):
                pass

            # Missing health_check

        incomplete = Incomplete()
        assert not isinstance(incomplete, NotificationBackend)
