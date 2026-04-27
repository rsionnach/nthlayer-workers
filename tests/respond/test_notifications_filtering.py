"""Tests for notification event filtering."""
from __future__ import annotations

from unittest.mock import MagicMock

from nthlayer_workers.respond.notifications import should_notify


def _make_context(events=None):
    """Build a mock context with notifications config."""
    ctx = MagicMock()
    if events is not None:
        ctx.metadata = {
            "service_context": {
                "spec": {
                    "notifications": {
                        "events": events,
                    }
                }
            }
        }
    else:
        ctx.metadata = {}
    return ctx


def test_should_notify_no_config_allows_all():
    """Without notifications config, all events are allowed."""
    ctx = _make_context(events=None)
    assert should_notify(ctx, "breach") is True
    assert should_notify(ctx, "correlation") is True
    assert should_notify(ctx, "verification") is True


def test_should_notify_filters_by_event_type():
    """Only listed event types are allowed."""
    ctx = _make_context(events=[
        {"type": "breach"},
        {"type": "resolution"},
    ])
    assert should_notify(ctx, "breach") is True
    assert should_notify(ctx, "resolution") is True
    assert should_notify(ctx, "correlation") is False
    assert should_notify(ctx, "incident") is False


def test_should_notify_severity_filter():
    """Event with severity filter only matches listed severities."""
    ctx = _make_context(events=[
        {"type": "breach", "severity": [1, 2]},
        {"type": "correlation"},
    ])
    assert should_notify(ctx, "breach", severity=1) is True
    assert should_notify(ctx, "breach", severity=2) is True
    assert should_notify(ctx, "breach", severity=3) is False
    assert should_notify(ctx, "correlation", severity=3) is True  # no severity filter


def test_should_notify_severity_none_matches_all():
    """When severity is None (not provided), it matches any severity filter."""
    ctx = _make_context(events=[
        {"type": "breach", "severity": [1, 2]},
    ])
    assert should_notify(ctx, "breach", severity=None) is True


def test_should_notify_all_six_defaults():
    """Default event set includes all six lifecycle events."""
    ctx = _make_context(events=None)
    for event_type in ("breach", "correlation", "incident", "remediation", "verification", "resolution"):
        assert should_notify(ctx, event_type) is True
