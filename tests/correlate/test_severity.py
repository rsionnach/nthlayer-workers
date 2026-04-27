"""Tests for severity pre-scoring."""
from __future__ import annotations
from nthlayer_workers.correlate.ingestion.severity import pre_score
from nthlayer_workers.correlate.types import EventType, SitRepEvent


def _make_event(value=None, threshold=None, severity=0.5):
    payload = {}
    if value is not None:
        payload["value"] = value
    if threshold is not None:
        payload["threshold"] = threshold
    return SitRepEvent(
        id="e1", timestamp="", source="prom", type=EventType.ALERT,
        service="api", environment="prod", severity=severity, payload=payload,
    )


class TestPreScore:
    def test_with_slo_above_threshold(self):
        event = _make_event(value=0.52, threshold=0.20)
        score = pre_score(event, {"api": {"latency_p99": 0.20}})
        assert score == 1.0  # (0.52 - 0.20) / 0.20 = 1.6 → capped at 1.0

    def test_with_slo_slightly_above(self):
        event = _make_event(value=0.22, threshold=0.20)
        score = pre_score(event, {"api": {"latency_p99": 0.20}})
        assert 0.0 < score < 0.5  # (0.22 - 0.20) / 0.20 = 0.1

    def test_without_slo_context(self):
        event = _make_event(severity=0.5)
        assert pre_score(event, None) == 0.5

    def test_service_not_in_targets(self):
        event = _make_event(value=1.0, threshold=0.5)
        assert pre_score(event, {"other": {}}) == 0.5

    def test_missing_value_in_payload(self):
        event = _make_event(threshold=0.20)
        assert pre_score(event, {"api": {}}) == 0.5

    def test_zero_threshold(self):
        event = _make_event(value=0.5, threshold=0)
        assert pre_score(event, {"api": {}}) == 0.5
