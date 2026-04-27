"""Tests for alert suppression — 'don't page me for this'."""

from datetime import timedelta

import pytest

from nthlayer_workers.respond.sre.suppression import (
    Suppression,
    check_suppression_override,
    create_suppression,
)


def _make_suppression(**overrides) -> Suppression:
    """Build a minimal Suppression for testing."""
    defaults = {
        "service": "payment-api",
        "metric": "latency_p99",
        "window": {"type": "daily", "start": "02:00", "end": "04:00"},
        "reason": "nightly backup",
        "baseline": 350.0,
        "override_threshold": 1050.0,
        "created_by": "human:rob",
    }
    defaults.update(overrides)
    return Suppression(**defaults)


class TestCreateSuppression:
    """Test suppression rule creation."""

    def test_creates_suppression_with_baseline(self):
        suppression = create_suppression(
            service="payment-api",
            metric="latency_p99",
            window={"type": "daily", "start": "02:00", "end": "04:00"},
            reason="nightly backup",
            baseline=350.0,
            override_multiplier=3.0,
            created_by="human:rob",
        )

        assert isinstance(suppression, Suppression)
        assert suppression.service == "payment-api"
        assert suppression.metric == "latency_p99"
        assert suppression.baseline == 350.0
        assert suppression.override_threshold == 1050.0
        assert suppression.reason == "nightly backup"
        assert suppression.created_by == "human:rob"

    def test_default_multiplier_is_3(self):
        suppression = create_suppression(
            service="payment-api",
            metric="latency_p99",
            window={"type": "daily", "start": "02:00", "end": "04:00"},
            reason="backup",
            baseline=100.0,
        )

        assert suppression.override_threshold == 300.0

    def test_window_stored(self):
        window = {"type": "daily", "start": "02:00", "end": "04:00", "timezone": "Europe/Dublin"}
        suppression = create_suppression(
            service="cache-service",
            metric="memory_usage",
            window=window,
            reason="gc cycle",
            baseline=500.0,
        )

        assert suppression.window == window

    def test_review_after_approximately_30_days(self):
        suppression = create_suppression(
            service="payment-api",
            metric="latency_p99",
            window={"type": "daily", "start": "02:00", "end": "04:00"},
            reason="backup",
            baseline=350.0,
        )

        assert suppression.review_after is not None
        assert suppression.created_at is not None
        delta = suppression.review_after - suppression.created_at
        assert timedelta(days=29) < delta <= timedelta(days=31)

    def test_negative_baseline_raises(self):
        with pytest.raises(ValueError, match="positive"):
            create_suppression(
                service="test",
                metric="test",
                window={},
                reason="test",
                baseline=-100.0,
            )

    def test_zero_baseline_raises(self):
        with pytest.raises(ValueError, match="positive"):
            create_suppression(
                service="test",
                metric="test",
                window={},
                reason="test",
                baseline=0.0,
            )


class TestCheckSuppressionOverride:
    """Test suppression override detection."""

    def test_within_threshold_not_overridden(self):
        assert check_suppression_override(_make_suppression(), 900.0) is False

    def test_exceeds_threshold_is_overridden(self):
        assert check_suppression_override(_make_suppression(), 1840.0) is True

    def test_exactly_at_threshold_not_overridden(self):
        assert check_suppression_override(_make_suppression(override_threshold=300.0), 300.0) is False
