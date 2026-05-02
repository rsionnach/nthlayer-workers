"""Tests for NL summary generation — SnapshotSummary, sample selection, generation."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from nthlayer_workers.correlate.summary import (
    SnapshotSummary,
    _select_sample_events,
    generate_summary,
)
from nthlayer_workers.correlate.types import EventType, SitRepEvent


def _event(
    service: str = "fraud-detect",
    severity: float = 0.5,
    timestamp: str = "2026-04-24T12:00:00+00:00",
    event_id: str = "evt-1",
    source: str = "assessment:slo_status",
    event_type: EventType = EventType.ALERT,
) -> SitRepEvent:
    return SitRepEvent(
        id=event_id,
        timestamp=timestamp,
        source=source,
        type=event_type,
        service=service,
        environment="production",
        severity=severity,
        payload={},
    )


class TestSnapshotSummaryModel:
    def test_valid_summary(self):
        s = SnapshotSummary(summary="Service X saw 3 SLO breaches over 2 minutes.")
        assert len(s.summary) <= 500
        assert s.notable_omissions == []

    def test_max_length_enforced(self):
        with pytest.raises(Exception):  # Pydantic ValidationError
            SnapshotSummary(summary="x" * 501)

    def test_default_omissions(self):
        s = SnapshotSummary(summary="Brief.")
        assert s.notable_omissions == []

    def test_with_omissions(self):
        s = SnapshotSummary(
            summary="Brief.",
            notable_omissions=["no trace data", "only 1 service observed"],
        )
        assert len(s.notable_omissions) == 2


class TestSelectSampleEvents:
    def test_basic_selection(self):
        events = [
            _event(event_id="e1", timestamp="2026-04-24T12:00:00+00:00", severity=0.3),
            _event(event_id="e2", timestamp="2026-04-24T12:01:00+00:00", severity=0.9),
            _event(event_id="e3", timestamp="2026-04-24T12:02:00+00:00", severity=0.5),
        ]
        samples = _select_sample_events(events)
        # Should include first (e1), most severe (e2), most recent for fraud-detect (e3)
        assert len(samples) >= 2
        services = {s["service"] for s in samples}
        assert "fraud-detect" in services

    def test_deduplicates(self):
        """Same event is first and most severe — appears once."""
        events = [
            _event(event_id="e1", timestamp="2026-04-24T12:00:00+00:00", severity=0.9),
            _event(event_id="e2", timestamp="2026-04-24T12:01:00+00:00", severity=0.5),
        ]
        samples = _select_sample_events(events)
        # e1 is both first and most severe — should appear once
        assert len(samples) == 2  # e1 + e2 (most recent for service)

    def test_caps_at_max(self):
        events = [
            _event(event_id=f"e{i}", service=f"svc-{i}", timestamp=f"2026-04-24T12:{i:02d}:00+00:00")
            for i in range(20)
        ]
        samples = _select_sample_events(events, max_samples=5)
        assert len(samples) <= 5

    def test_empty_events(self):
        assert _select_sample_events([]) == []

    def test_multiple_services(self):
        events = [
            _event(event_id="e1", service="svc-a", timestamp="2026-04-24T12:00:00+00:00"),
            _event(event_id="e2", service="svc-b", timestamp="2026-04-24T12:01:00+00:00"),
            _event(event_id="e3", service="svc-a", timestamp="2026-04-24T12:02:00+00:00"),
        ]
        samples = _select_sample_events(events)
        services = {s["service"] for s in samples}
        assert "svc-a" in services
        assert "svc-b" in services


class TestGenerateSummary:
    @patch("nthlayer_workers.correlate.summary.structured_call")
    async def test_success(self, mock_call):
        mock_call.return_value = SnapshotSummary(
            summary="fraud-detect experienced 3 SLO breaches over 2 minutes.",
            notable_omissions=["no trace data available"],
        )

        snapshot_data = {
            "domain": {"service": "fraud-detect", "environment": "production"},
            "window": {"duration_seconds": 120, "close_reason": "gap"},
            "event_count": 3,
            "event_types": {"alert": 2, "quality_score": 1},
            "peak_severity": 0.9,
            "affected_services": ["fraud-detect"],
        }
        events = [_event()]

        result = await generate_summary(snapshot_data, events)
        assert result is not None
        assert "fraud-detect" in result["summary"]
        assert "no trace data" in result["notable_omissions"][0]

    @patch("nthlayer_workers.correlate.summary.structured_call")
    async def test_timeout_returns_none(self, mock_call):
        """LLM hangs → returns None within timeout, no crash."""
        import time

        # Blocks the executor thread; asyncio.wait_for cancels the coroutine
        mock_call.side_effect = lambda *a, **k: time.sleep(10)

        snapshot_data = {
            "domain": {"service": "svc", "environment": "production"},
            "window": {"duration_seconds": 60, "close_reason": "gap"},
            "event_count": 1,
            "event_types": {},
            "peak_severity": 0.5,
            "affected_services": ["svc"],
        }

        # Use a shorter timeout for the test
        from nthlayer_workers.correlate import summary
        original_timeout = summary.SUMMARY_TIMEOUT
        summary.SUMMARY_TIMEOUT = 0.1
        try:
            result = await generate_summary(snapshot_data, [_event()])
            assert result is None
        finally:
            summary.SUMMARY_TIMEOUT = original_timeout

    @patch("nthlayer_workers.correlate.summary.structured_call")
    async def test_llm_unavailable_returns_none(self, mock_call):
        from nthlayer_common.llm import LLMError
        mock_call.side_effect = LLMError("no API key", provider="anthropic", model="claude")

        result = await generate_summary(
            {"domain": {"service": "svc"}, "window": {}, "event_count": 0,
             "event_types": {}, "peak_severity": 0, "affected_services": []},
            [],
        )
        assert result is None

    @patch("nthlayer_workers.correlate.summary.structured_call")
    async def test_validation_error_returns_none(self, mock_call):
        mock_call.side_effect = Exception("ValidationError: summary too long")

        result = await generate_summary(
            {"domain": {"service": "svc"}, "window": {}, "event_count": 0,
             "event_types": {}, "peak_severity": 0, "affected_services": []},
            [],
        )
        assert result is None
