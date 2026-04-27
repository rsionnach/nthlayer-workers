"""Tests for the decision records bridge — converting observe assessments to content-addressed records."""

from datetime import datetime, timezone


from nthlayer_workers.observe.assessment import Assessment as LegacyAssessment
from nthlayer_workers.observe.decision_records import (
    build_decision_record,
    map_severity,
    build_stream,
    generate_summaries,
)
from nthlayer_common.records.models import (
    Assessment as DecisionAssessment,
    AssessmentType,
    Severity,
    Summaries,
    ZERO_HASH,
)
from nthlayer_common.records.hashing import verify_hash

NOW = datetime(2026, 4, 11, 12, 0, 0, tzinfo=timezone.utc)


# --- Fixtures ---


def _legacy_slo_status(service="checkout-service", **data_overrides):
    data = {
        "slo_name": "availability",
        "objective": 99.9,
        "window": "30d",
        "total_budget_minutes": 43.2,
        "status": "CRITICAL",
        "current_sli": 99.85,
        "burned_minutes": 38.8,
        "percent_consumed": 89.8,
        "error": None,
    }
    data.update(data_overrides)
    return LegacyAssessment(
        id="asm-2026-04-11-abcd1234-00001",
        created_at=NOW,
        kind="slo_status",
        service=service,
        producer="nthlayer-observe",
        data=data,
    )


def _legacy_drift(service="checkout-service", **data_overrides):
    data = {
        "slo_name": "availability",
        "severity": "warn",
        "pattern": "gradual_decline",
        "slope_per_week": -0.3,
        "days_until_exhaustion": 12,
        "current_budget": 65.2,
        "summary": "Budget declining at 0.3%/week",
        "recommendation": "Review recent deployment changes",
    }
    data.update(data_overrides)
    return LegacyAssessment(
        id="asm-2026-04-11-abcd1234-00002",
        created_at=NOW,
        kind="drift_signal",
        service=service,
        producer="nthlayer-observe",
        data=data,
    )


def _legacy_verification(service="checkout-service", **data_overrides):
    data = {
        "declared_metrics": 5,
        "found_metrics": 4,
        "missing_critical": ["http_request_duration_seconds"],
        "missing_optional": [],
        "exit_code": 2,
    }
    data.update(data_overrides)
    return LegacyAssessment(
        id="asm-2026-04-11-abcd1234-00003",
        created_at=NOW,
        kind="verification",
        service=service,
        producer="nthlayer-observe",
        data=data,
    )


def _legacy_gate(service="checkout-service", **data_overrides):
    data = {
        "action": "deploy",
        "decision": "blocked",
        "budget_remaining_pct": 8.5,
        "warning_threshold": 20.0,
        "blocking_threshold": 10.0,
        "slo_count": 3,
        "reasons": ["Budget exhausted below blocking threshold"],
    }
    data.update(data_overrides)
    return LegacyAssessment(
        id="asm-2026-04-11-abcd1234-00004",
        created_at=NOW,
        kind="deploy_gate",
        service=service,
        producer="nthlayer-observe",
        data=data,
    )


def _legacy_dependency(service="checkout-service", **data_overrides):
    data = {
        "dependencies_discovered": 3,
        "upstream": [{"service": "payment-api", "type": "service", "provider": "prometheus"}],
        "downstream": [{"service": "mobile-gateway", "type": "service", "provider": "prometheus"}],
        "providers_queried": ["prometheus"],
        "errors": [],
    }
    data.update(data_overrides)
    return LegacyAssessment(
        id="asm-2026-04-11-abcd1234-00005",
        created_at=NOW,
        kind="dependency_graph",
        service=service,
        producer="nthlayer-observe",
        data=data,
    )


# --- Severity Mapping ---


class TestMapSeverity:
    def test_slo_status_exhausted(self):
        assert map_severity("slo_status", {"status": "EXHAUSTED"}) == Severity.CRITICAL

    def test_slo_status_critical(self):
        assert map_severity("slo_status", {"status": "CRITICAL"}) == Severity.CRITICAL

    def test_slo_status_warning(self):
        assert map_severity("slo_status", {"status": "WARNING"}) == Severity.WARNING

    def test_slo_status_healthy(self):
        assert map_severity("slo_status", {"status": "HEALTHY"}) == Severity.INFO

    def test_slo_status_no_data(self):
        assert map_severity("slo_status", {"status": "NO_DATA"}) == Severity.WARNING

    def test_slo_status_error(self):
        assert map_severity("slo_status", {"status": "ERROR"}) == Severity.WARNING

    def test_drift_critical(self):
        assert map_severity("drift_signal", {"severity": "critical"}) == Severity.CRITICAL

    def test_drift_warn(self):
        assert map_severity("drift_signal", {"severity": "warn"}) == Severity.WARNING

    def test_drift_info(self):
        assert map_severity("drift_signal", {"severity": "info"}) == Severity.INFO

    def test_drift_none(self):
        assert map_severity("drift_signal", {"severity": "none"}) == Severity.INFO

    def test_verification_exit_2(self):
        assert map_severity("verification", {"exit_code": 2}) == Severity.CRITICAL

    def test_verification_exit_1(self):
        assert map_severity("verification", {"exit_code": 1}) == Severity.WARNING

    def test_verification_exit_0(self):
        assert map_severity("verification", {"exit_code": 0}) == Severity.INFO

    def test_gate_blocked(self):
        assert map_severity("deploy_gate", {"decision": "blocked"}) == Severity.CRITICAL

    def test_gate_warning(self):
        assert map_severity("deploy_gate", {"decision": "warning"}) == Severity.WARNING

    def test_gate_approved(self):
        assert map_severity("deploy_gate", {"decision": "approved"}) == Severity.INFO

    def test_dependency_with_errors(self):
        assert map_severity("dependency_graph", {"errors": ["connection failed"]}) == Severity.WARNING

    def test_dependency_no_errors(self):
        assert map_severity("dependency_graph", {"errors": []}) == Severity.INFO


# --- Stream Construction ---


class TestBuildStream:
    def test_slo_status_stream(self):
        a = _legacy_slo_status()
        assert build_stream(a) == "sli:checkout-service:availability"

    def test_drift_stream(self):
        a = _legacy_drift()
        assert build_stream(a) == "sli:checkout-service:availability"

    def test_verification_stream(self):
        a = _legacy_verification()
        assert build_stream(a) == "verification:checkout-service"

    def test_gate_stream(self):
        a = _legacy_gate()
        assert build_stream(a) == "deploy_gate:checkout-service"

    def test_dependency_stream(self):
        a = _legacy_dependency()
        assert build_stream(a) == "dependency_graph:checkout-service"


# --- Summary Generation ---


class TestGenerateSummaries:
    def test_slo_status_summaries(self):
        a = _legacy_slo_status()
        s = generate_summaries(a)
        assert isinstance(s, Summaries)
        assert "availability" in s.technical
        assert "89.8%" in s.technical
        assert len(s.technical) <= 280
        assert len(s.plain) <= 280
        assert s.executive is not None
        assert len(s.executive) <= 140

    def test_drift_summaries(self):
        a = _legacy_drift()
        s = generate_summaries(a)
        assert "availability" in s.technical
        assert "decline" in s.technical.lower() or "0.3" in s.technical

    def test_verification_summaries(self):
        a = _legacy_verification()
        s = generate_summaries(a)
        assert "4" in s.technical and "5" in s.technical  # found/declared
        assert "missing" in s.technical.lower() or "critical" in s.technical.lower()

    def test_gate_summaries(self):
        a = _legacy_gate()
        s = generate_summaries(a)
        assert "blocked" in s.technical.lower() or "BLOCKED" in s.technical

    def test_dependency_summaries(self):
        a = _legacy_dependency()
        s = generate_summaries(a)
        assert "3" in s.technical  # dependencies_discovered

    def test_summaries_within_char_limits(self):
        """All summary registers stay within spec limits."""
        for factory in [_legacy_slo_status, _legacy_drift, _legacy_verification, _legacy_gate, _legacy_dependency]:
            a = factory()
            s = generate_summaries(a)
            assert len(s.technical) <= 280, f"{a.kind} technical too long"
            assert len(s.plain) <= 280, f"{a.kind} plain too long"
            if s.executive:
                assert len(s.executive) <= 140, f"{a.kind} executive too long"


# --- Full Record Building ---


class TestBuildDecisionRecord:
    def test_builds_valid_assessment(self):
        legacy = _legacy_slo_status()
        record = build_decision_record(legacy, previous_hash=ZERO_HASH)
        assert isinstance(record, DecisionAssessment)
        assert record.schema_version == "assessment/v1"
        assert record.stream == "sli:checkout-service:availability"
        assert record.type == AssessmentType.THRESHOLD_BREACH
        assert record.severity == Severity.CRITICAL
        assert record.previous_hash == ZERO_HASH
        assert record.incident_id is None

    def test_hash_is_valid(self):
        legacy = _legacy_slo_status()
        record = build_decision_record(legacy, previous_hash=ZERO_HASH)
        assert verify_hash(record) is True

    def test_drift_maps_to_drift_type(self):
        legacy = _legacy_drift()
        record = build_decision_record(legacy, previous_hash=ZERO_HASH)
        assert record.type == AssessmentType.DRIFT

    def test_gate_maps_to_change_event_type(self):
        legacy = _legacy_gate()
        record = build_decision_record(legacy, previous_hash=ZERO_HASH)
        assert record.type == AssessmentType.CHANGE_EVENT

    def test_verification_maps_to_change_event_type(self):
        legacy = _legacy_verification()
        record = build_decision_record(legacy, previous_hash=ZERO_HASH)
        assert record.type == AssessmentType.CHANGE_EVENT

    def test_dependency_maps_to_change_event_type(self):
        legacy = _legacy_dependency()
        record = build_decision_record(legacy, previous_hash=ZERO_HASH)
        assert record.type == AssessmentType.CHANGE_EVENT

    def test_preserves_timestamp(self):
        legacy = _legacy_slo_status()
        record = build_decision_record(legacy, previous_hash=ZERO_HASH)
        assert record.timestamp == NOW

    def test_payload_matches_data(self):
        legacy = _legacy_slo_status()
        record = build_decision_record(legacy, previous_hash=ZERO_HASH)
        assert record.payload == legacy.data

    def test_incident_id_passthrough(self):
        legacy = _legacy_slo_status()
        record = build_decision_record(legacy, previous_hash=ZERO_HASH, incident_id="inc-001")
        assert record.incident_id == "inc-001"

    def test_different_data_different_hash(self):
        a1 = _legacy_slo_status(status="CRITICAL")
        a2 = _legacy_slo_status(status="HEALTHY")
        r1 = build_decision_record(a1, previous_hash=ZERO_HASH)
        r2 = build_decision_record(a2, previous_hash=ZERO_HASH)
        assert r1.hash != r2.hash
