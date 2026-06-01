"""Tests for post-incident review — auto-generated from verdict chain."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

from nthlayer_workers.respond.sre.post_incident import (
    PostIncidentReview,
    TimelineEntry,
    build_accuracy_section,
    build_review_sections,
    build_timeline,
    render_post_incident,
)


def _make_verdict(
    *,
    subject_type: str = "triage",
    producer_system: str = "nthlayer-respond",
    producer_instance: str = "triage",
    service: str = "fraud-detect",
    summary: str = "SEV-2: breach",
    action: str = "flag",
    confidence: float = 0.85,
    status: str = "confirmed",
    timestamp: datetime | None = None,
    custom: dict | None = None,
) -> MagicMock:
    v = MagicMock()
    v.subject.type = subject_type
    v.subject.ref = service
    v.subject.summary = summary
    v.producer.system = producer_system
    v.producer.instance = producer_instance
    v.judgment.action = action
    v.judgment.confidence = confidence
    v.judgment.reasoning = summary
    v.outcome.status = status
    v.outcome.override = None
    v.metadata.custom = custom or {}
    v.timestamp = timestamp or datetime(2026, 4, 13, 14, 22, 0, tzinfo=UTC)
    v.id = f"vrd-{subject_type}-001"
    return v


class TestBuildTimeline:
    """Test timeline reconstruction from verdicts."""

    def test_returns_sorted_entries(self):
        v1 = _make_verdict(
            subject_type="evaluation",
            producer_system="nthlayer-measure",
            timestamp=datetime(2026, 4, 13, 14, 10, tzinfo=UTC),
            summary="Reversal rate breach detected",
        )
        v2 = _make_verdict(
            subject_type="correlation",
            producer_system="nthlayer-correlate",
            timestamp=datetime(2026, 4, 13, 14, 22, tzinfo=UTC),
            summary="Deploy v2.3.1 correlated",
        )
        v3 = _make_verdict(
            subject_type="triage",
            timestamp=datetime(2026, 4, 13, 14, 23, tzinfo=UTC),
            summary="P2, blast radius: fraud-detect + payment-api",
        )

        timeline = build_timeline([v1, v2, v3])

        assert len(timeline) == 3
        assert isinstance(timeline[0], TimelineEntry)
        assert timeline[0].timestamp < timeline[1].timestamp
        assert timeline[1].timestamp < timeline[2].timestamp

    def test_empty_verdicts_produces_empty_timeline(self):
        assert build_timeline([]) == []


class TestBuildReviewSections:
    """Test 'what worked' / 'what to improve' sections."""

    def test_confirmed_verdicts_in_worked(self):
        v = _make_verdict(status="confirmed", producer_instance="triage")
        worked, improve = build_review_sections([v])

        assert len(worked) == 1
        assert "confirmed" in worked[0].lower()

    def test_overridden_verdicts_in_improve(self):
        v = _make_verdict(status="overridden", producer_instance="triage")
        v.outcome.override = MagicMock()
        v.outcome.override.reasoning = "Severity was too low"
        worked, improve = build_review_sections([v])

        assert len(improve) == 1
        assert "overridden" in improve[0].lower() or "Severity" in improve[0]

    def test_pending_excluded(self):
        v = _make_verdict(status="pending")
        worked, improve = build_review_sections([v])

        assert len(worked) == 0
        assert len(improve) == 0


class TestBuildAccuracySection:
    """Test per-verdict accuracy display."""

    def test_confirmed_shows_checkmark(self):
        v = _make_verdict(
            producer_system="nthlayer-respond",
            producer_instance="triage",
            status="confirmed",
        )
        lines = build_accuracy_section([v])

        assert len(lines) >= 1
        assert "confirmed" in lines[0].lower() or "\u2713" in lines[0]

    def test_overridden_shows_cross(self):
        v = _make_verdict(
            producer_system="nthlayer-respond",
            producer_instance="remediation",
            status="overridden",
        )
        v.outcome.override = MagicMock()
        v.outcome.override.reasoning = "Wrong action"
        lines = build_accuracy_section([v])

        assert len(lines) >= 1
        assert "overridden" in lines[0].lower() or "\u2717" in lines[0]

    def test_excludes_non_agent_verdicts(self):
        v = _make_verdict(
            producer_system="human",
            producer_instance="manual",
            status="confirmed",
        )
        lines = build_accuracy_section([v])

        assert len(lines) == 0


class TestRenderPostIncident:
    """Test full post-incident review rendering."""

    def test_includes_timeline(self):
        review = PostIncidentReview(
            incident_id="INC-001",
            timeline=[
                TimelineEntry(
                    timestamp=datetime(2026, 4, 13, 14, 10, tzinfo=UTC),
                    source="nthlayer-measure",
                    agent="evaluation",
                    summary="Breach detected",
                    confidence=0.95,
                ),
            ],
            worked=["Triage severity was accurate (confirmed)"],
            improve=[],
            accuracy=["  triage: confirmed \u2713"],
        )

        text = render_post_incident(review)

        assert "INC-001" in text
        assert "Timeline" in text
        assert "Breach detected" in text
        assert "What worked" in text

    def test_includes_improve_section(self):
        review = PostIncidentReview(
            incident_id="INC-001",
            timeline=[],
            worked=[],
            improve=["Detection took 12 minutes"],
            accuracy=[],
        )

        text = render_post_incident(review)

        assert "improve" in text.lower()
        assert "12 minutes" in text
