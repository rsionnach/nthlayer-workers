"""Tests for shift report — summary of what happened during an on-call shift."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

from nthlayer_workers.respond.sre.shift_report import (
    ShiftReport,
    build_shift_report,
    is_quiet,
    render_shift_report,
)


def _make_verdict(
    *,
    subject_type: str = "triage",
    producer_system: str = "nthlayer-respond",
    service: str = "fraud-detect",
    summary: str = "SEV-2: breach",
    action: str = "flag",
    confidence: float = 0.85,
    status: str = "confirmed",
    custom: dict | None = None,
    timestamp: datetime | None = None,
) -> MagicMock:
    v = MagicMock()
    v.subject.type = subject_type
    v.subject.ref = service
    v.subject.summary = summary
    v.producer.system = producer_system
    v.judgment.action = action
    v.judgment.confidence = confidence
    v.judgment.tags = []
    v.outcome.status = status
    v.metadata.custom = custom or {}
    v.timestamp = timestamp or datetime(2026, 4, 13, 14, 0, 0, tzinfo=timezone.utc)
    v.id = f"vrd-{subject_type}-001"
    return v


def _make_store(*verdicts) -> MagicMock:
    store = MagicMock()

    def _query(criteria):
        results = []
        for v in verdicts:
            if criteria.subject_type and v.subject.type != criteria.subject_type:
                continue
            if criteria.producer_system and v.producer.system != criteria.producer_system:
                continue
            if criteria.status and v.outcome.status != criteria.status:
                continue
            if criteria.from_time and v.timestamp < criteria.from_time:
                continue
            if criteria.to_time and v.timestamp > criteria.to_time:
                continue
            results.append(v)
        return results

    store.query.side_effect = _query
    return store


class TestBuildShiftReport:
    """Test shift report construction from verdicts."""

    def test_returns_shift_report(self):
        store = _make_store()
        shift_start = datetime(2026, 4, 13, 9, 0, tzinfo=timezone.utc)
        shift_end = datetime(2026, 4, 14, 9, 0, tzinfo=timezone.utc)

        report = build_shift_report(shift_start, shift_end, store)

        assert isinstance(report, ShiftReport)
        assert report.window_start == shift_start
        assert report.window_end == shift_end

    def test_quiet_shift(self):
        store = _make_store()
        shift_start = datetime(2026, 4, 13, 9, 0, tzinfo=timezone.utc)
        shift_end = datetime(2026, 4, 14, 9, 0, tzinfo=timezone.utc)

        report = build_shift_report(shift_start, shift_end, store)

        assert is_quiet(report)
        assert len(report.incidents) == 0

    def test_busy_shift_with_triage_incident(self):
        """Triage verdicts are counted as incidents."""
        incident = _make_verdict(
            subject_type="triage",
            producer_system="nthlayer-respond",
            custom={"severity": 2},
            timestamp=datetime(2026, 4, 13, 14, 22, tzinfo=timezone.utc),
        )
        store = _make_store(incident)
        shift_start = datetime(2026, 4, 13, 9, 0, tzinfo=timezone.utc)
        shift_end = datetime(2026, 4, 14, 9, 0, tzinfo=timezone.utc)

        report = build_shift_report(shift_start, shift_end, store)

        assert not is_quiet(report)
        assert len(report.incidents) == 1

    def test_busy_shift_with_custom_incident(self):
        """Custom verdicts with incident_type are also counted."""
        incident = _make_verdict(
            subject_type="custom",
            custom={"incident_type": "incident", "severity": 2, "duration": "16 minutes"},
            timestamp=datetime(2026, 4, 13, 14, 22, tzinfo=timezone.utc),
        )
        store = _make_store(incident)
        shift_start = datetime(2026, 4, 13, 9, 0, tzinfo=timezone.utc)
        shift_end = datetime(2026, 4, 14, 9, 0, tzinfo=timezone.utc)

        report = build_shift_report(shift_start, shift_end, store)

        assert not is_quiet(report)
        assert len(report.incidents) == 1

    def test_counts_deploys(self):
        deploy = _make_verdict(
            subject_type="evaluation",
            producer_system="nthlayer-measure",
            custom={"slo_type": "judgment", "breach": False},
            timestamp=datetime(2026, 4, 13, 16, 0, tzinfo=timezone.utc),
        )
        store = _make_store(deploy)
        shift_start = datetime(2026, 4, 13, 9, 0, tzinfo=timezone.utc)
        shift_end = datetime(2026, 4, 14, 9, 0, tzinfo=timezone.utc)

        report = build_shift_report(shift_start, shift_end, store)

        assert report.evaluations_count >= 1

    def test_pending_reviews(self):
        pending = _make_verdict(status="pending")
        store = _make_store(pending)
        shift_start = datetime(2026, 4, 13, 9, 0, tzinfo=timezone.utc)
        shift_end = datetime(2026, 4, 14, 9, 0, tzinfo=timezone.utc)

        report = build_shift_report(shift_start, shift_end, store)

        assert report.pending_reviews >= 1

    def test_excludes_verdicts_outside_window(self):
        outside = _make_verdict(
            subject_type="triage",
            producer_system="nthlayer-respond",
            timestamp=datetime(2026, 4, 12, 8, 0, tzinfo=timezone.utc),  # Before shift
        )
        store = _make_store(outside)
        shift_start = datetime(2026, 4, 13, 9, 0, tzinfo=timezone.utc)
        shift_end = datetime(2026, 4, 14, 9, 0, tzinfo=timezone.utc)

        report = build_shift_report(shift_start, shift_end, store)

        assert len(report.incidents) == 0


class TestIsQuiet:
    """Test quiet shift detection."""

    def test_quiet_when_empty(self):
        report = ShiftReport(
            window_start=datetime(2026, 4, 13, 9, 0, tzinfo=timezone.utc),
            window_end=datetime(2026, 4, 14, 9, 0, tzinfo=timezone.utc),
        )
        assert is_quiet(report)

    def test_not_quiet_with_incidents(self):
        report = ShiftReport(
            window_start=datetime(2026, 4, 13, 9, 0, tzinfo=timezone.utc),
            window_end=datetime(2026, 4, 14, 9, 0, tzinfo=timezone.utc),
            incidents=[MagicMock()],
        )
        assert not is_quiet(report)

    def test_not_quiet_with_pending_reviews(self):
        report = ShiftReport(
            window_start=datetime(2026, 4, 13, 9, 0, tzinfo=timezone.utc),
            window_end=datetime(2026, 4, 14, 9, 0, tzinfo=timezone.utc),
            pending_reviews=3,
        )
        assert not is_quiet(report)


class TestRenderShiftReport:
    """Test shift report rendering."""

    def test_quiet_shift_render(self):
        report = ShiftReport(
            window_start=datetime(2026, 4, 13, 9, 0, tzinfo=timezone.utc),
            window_end=datetime(2026, 4, 14, 9, 0, tzinfo=timezone.utc),
        )
        text = render_shift_report(report)

        assert "Nothing required your attention" in text or "quiet" in text.lower()

    def test_busy_shift_render(self):
        incident = _make_verdict(
            subject_type="custom",
            custom={"incident_type": "incident", "severity": 2},
        )
        report = ShiftReport(
            window_start=datetime(2026, 4, 13, 9, 0, tzinfo=timezone.utc),
            window_end=datetime(2026, 4, 14, 9, 0, tzinfo=timezone.utc),
            incidents=[incident],
            evaluations_count=3,
            pending_reviews=2,
        )
        text = render_shift_report(report)

        assert "1 incident" in text
        assert "pending" in text.lower()
