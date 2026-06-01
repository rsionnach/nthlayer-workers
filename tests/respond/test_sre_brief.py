"""Tests for paging brief — template over verdicts."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

from nthlayer_workers.respond.sre.brief import PagingBrief, build_paging_brief, render_brief


def _make_verdict(
    *,
    subject_type: str = "triage",
    producer_system: str = "nthlayer-respond",
    service: str = "fraud-detect",
    summary: str = "SEV-2: reversal rate breach",
    action: str = "flag",
    confidence: float = 0.85,
    reasoning: str = "Reversal rate at 8%, target 1.5%.",
    tags: list[str] | None = None,
    custom: dict | None = None,
) -> MagicMock:
    """Build a mock verdict for testing."""
    v = MagicMock()
    v.subject.type = subject_type
    v.subject.ref = service
    v.subject.service = service
    v.subject.summary = summary
    v.producer.system = producer_system
    v.judgment.action = action
    v.judgment.confidence = confidence
    v.judgment.reasoning = reasoning
    v.judgment.tags = tags or []
    v.judgment.dimensions = {}
    v.metadata.custom = custom or {}
    v.timestamp = datetime(2026, 4, 13, 14, 22, 0, tzinfo=UTC)
    v.id = f"vrd-{subject_type}-001"
    v.lineage.context = []
    return v


def _make_store(*verdicts) -> MagicMock:
    """Build a mock verdict store that returns given verdicts on query."""
    store = MagicMock()

    def _query(criteria):
        results = []
        for v in verdicts:
            if criteria.subject_type and v.subject.type != criteria.subject_type:
                continue
            if criteria.producer_system and v.producer.system != criteria.producer_system:
                continue
            results.append(v)
        return results

    store.query.side_effect = _query
    # by_lineage returns all verdicts (simplified mock)
    store.by_lineage.return_value = list(verdicts)
    return store


class TestBuildPagingBrief:
    """Test build_paging_brief query logic."""

    def test_returns_paging_brief(self):
        triage = _make_verdict(
            subject_type="triage",
            custom={"severity": 2, "blast_radius": ["fraud-detect"]},
        )
        correlation = _make_verdict(
            subject_type="correlation",
            producer_system="nthlayer-correlate",
            summary="Root cause: deploy v2.3.1",
            confidence=0.74,
            custom={"root_causes": [{"service": "fraud-detect", "type": "deploy"}]},
        )
        store = _make_store(triage, correlation)

        brief = build_paging_brief("INC-FRAUD-001", store)

        assert isinstance(brief, PagingBrief)
        assert brief.incident_id == "INC-FRAUD-001"
        assert brief.service == "fraud-detect"
        assert brief.severity == 2

    def test_extracts_triage_fields(self):
        triage = _make_verdict(
            subject_type="triage",
            summary="SEV-1: fraud-detect reversal rate breach",
            custom={"severity": 1, "blast_radius": ["fraud-detect", "payment-api"]},
        )
        store = _make_store(triage)

        brief = build_paging_brief("INC-001", store)

        assert brief.severity == 1
        assert brief.blast_radius == ["fraud-detect", "payment-api"]

    def test_extracts_correlation_root_cause(self):
        triage = _make_verdict(subject_type="triage")
        correlation = _make_verdict(
            subject_type="correlation",
            producer_system="nthlayer-correlate",
            confidence=0.74,
            reasoning="Deploy v2.3.1 to fraud-detect 14 minutes ago",
            custom={"root_causes": [{"service": "fraud-detect", "type": "deploy"}]},
        )
        store = _make_store(triage, correlation)

        brief = build_paging_brief("INC-001", store)

        assert brief.likely_cause is not None
        assert "deploy" in brief.likely_cause.lower() or "v2.3.1" in brief.likely_cause.lower()
        assert brief.cause_confidence == 0.74

    def test_extracts_remediation(self):
        triage = _make_verdict(subject_type="triage")
        remediation = _make_verdict(
            subject_type="remediation",
            summary="rollback on fraud-detect",
            custom={"proposed_action": "rollback", "target": "fraud-detect"},
        )
        store = _make_store(triage, remediation)

        brief = build_paging_brief("INC-001", store)

        assert brief.recommended_action == "rollback"

    def test_missing_remediation_handled(self):
        triage = _make_verdict(subject_type="triage")
        store = _make_store(triage)

        brief = build_paging_brief("INC-001", store)

        assert brief.recommended_action is None

    def test_missing_correlation_handled(self):
        triage = _make_verdict(subject_type="triage")
        store = _make_store(triage)

        brief = build_paging_brief("INC-001", store)

        assert brief.likely_cause is None
        assert brief.cause_confidence is None


class TestRenderBrief:
    """Test brief rendering to text."""

    def test_render_includes_severity_emoji(self):
        brief = PagingBrief(
            incident_id="INC-001",
            service="fraud-detect",
            severity=2,
            summary="Reversal rate at 8%, target 1.5%.",
            likely_cause="Deploy v2.3.1",
            cause_confidence=0.74,
            blast_radius=["fraud-detect", "payment-api"],
            recommended_action="rollback",
        )

        text = render_brief(brief)

        assert "\U0001f7e0" in text or "P2" in text  # orange circle or P2 label
        assert "fraud-detect" in text
        assert "Reversal rate" in text

    def test_render_includes_blast_radius(self):
        brief = PagingBrief(
            incident_id="INC-001",
            service="fraud-detect",
            severity=1,
            summary="Down.",
            blast_radius=["fraud-detect", "payment-api", "checkout-svc"],
        )

        text = render_brief(brief)

        assert "fraud-detect" in text
        assert "payment-api" in text

    def test_render_without_cause(self):
        brief = PagingBrief(
            incident_id="INC-001",
            service="fraud-detect",
            severity=3,
            summary="Minor issue.",
        )

        text = render_brief(brief)

        assert "INC-001" in text
        assert "Investigation in progress" in text or "cause" not in text.lower()

    def test_render_without_remediation(self):
        brief = PagingBrief(
            incident_id="INC-001",
            service="fraud-detect",
            severity=2,
            summary="Issue.",
        )

        text = render_brief(brief)

        assert "INC-001" in text

    def test_render_with_remediation(self):
        brief = PagingBrief(
            incident_id="INC-001",
            service="fraud-detect",
            severity=2,
            summary="Issue.",
            recommended_action="rollback",
        )

        text = render_brief(brief)

        assert "rollback" in text.lower()
