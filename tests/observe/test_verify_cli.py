"""Tests for the verify subcommand — chain and incident verification via CLI."""

from datetime import UTC, datetime

from nthlayer_common.records.hashing import canonical_json, compute_hash
from nthlayer_common.records.models import (
    ZERO_HASH,
    Assessment,
    AssessmentType,
    Evaluation,
    EvaluationMethod,
    EvaluationOutcome,
    Incident,
    Severity,
    Summaries,
    Verdict,
    VerdictOutcome,
)
from nthlayer_common.records.sqlite_store import SQLiteDecisionRecordStore

from nthlayer_workers.observe.cli import main

NOW = datetime(2026, 4, 11, 12, 0, 0, tzinfo=UTC)
T1 = datetime(2026, 4, 11, 12, 1, 0, tzinfo=UTC)
T2 = datetime(2026, 4, 11, 12, 2, 0, tzinfo=UTC)
T3 = datetime(2026, 4, 11, 12, 3, 0, tzinfo=UTC)


def _hashed(record):
    """Compute correct hash for a record built with hash='placeholder'."""
    canonical = canonical_json(record)
    return compute_hash(canonical)


def _make_assessment(previous_hash=ZERO_HASH, timestamp=NOW, **overrides):
    a = Assessment(
        hash="placeholder",
        previous_hash=previous_hash,
        schema_version="assessment/v1",
        timestamp=timestamp,
        stream=overrides.pop("stream", "sli:checkout:latency-p99"),
        incident_id=overrides.pop("incident_id", None),
        type=overrides.pop("type", AssessmentType.THRESHOLD_BREACH),
        severity=overrides.pop("severity", Severity.CRITICAL),
        payload=overrides.pop("payload", {"current_value": 1247}),
        summaries=overrides.pop("summaries", Summaries(technical="t", plain="p", executive="e")),
    )
    return Assessment(hash=_hashed(a), previous_hash=a.previous_hash, schema_version=a.schema_version,
                      timestamp=a.timestamp, stream=a.stream, incident_id=a.incident_id,
                      type=a.type, severity=a.severity, payload=a.payload, summaries=a.summaries)


def _make_verdict(previous_hash=ZERO_HASH, timestamp=NOW, **overrides):
    v = Verdict(
        hash="placeholder",
        previous_hash=previous_hash,
        schema_version="verdict/v1",
        timestamp=timestamp,
        agent=overrides.pop("agent", "triage"),
        incident_id=overrides.pop("incident_id", "inc-001"),
        input_hashes=overrides.pop("input_hashes", []),
        prompt_hash=overrides.pop("prompt_hash", "d" * 64),
        response_hash=overrides.pop("response_hash", "e" * 64),
        model=overrides.pop("model", "test-model"),
        reasoning=overrides.pop("reasoning", "test"),
        action=overrides.pop("action", {}),
        outcome=overrides.pop("outcome", VerdictOutcome.RECOMMENDED),
        summaries=overrides.pop("summaries", Summaries(technical="t", plain="p", executive="e")),
    )
    return Verdict(hash=_hashed(v), previous_hash=v.previous_hash, schema_version=v.schema_version,
                   timestamp=v.timestamp, agent=v.agent, incident_id=v.incident_id,
                   input_hashes=v.input_hashes, prompt_hash=v.prompt_hash,
                   response_hash=v.response_hash, model=v.model, reasoning=v.reasoning,
                   action=v.action, outcome=v.outcome, summaries=v.summaries)


def _make_evaluation(previous_hash=ZERO_HASH, timestamp=NOW, **overrides):
    e = Evaluation(
        hash="placeholder",
        previous_hash=previous_hash,
        schema_version="evaluation/v1",
        timestamp=timestamp,
        incident_id=overrides.pop("incident_id", "inc-001"),
        verdict_hash=overrides.pop("verdict_hash", "b" * 64),
        method=overrides.pop("method", EvaluationMethod.METRIC_RECOVERY),
        outcome=overrides.pop("outcome", EvaluationOutcome.EFFECTIVE),
        evidence_hashes=overrides.pop("evidence_hashes", []),
        payload=overrides.pop("payload", {}),
        summaries=overrides.pop("summaries", Summaries(technical="t", plain="p")),
    )
    return Evaluation(hash=_hashed(e), previous_hash=e.previous_hash, schema_version=e.schema_version,
                      timestamp=e.timestamp, incident_id=e.incident_id, verdict_hash=e.verdict_hash,
                      method=e.method, outcome=e.outcome, evidence_hashes=e.evidence_hashes,
                      payload=e.payload, summaries=e.summaries)


class TestVerifyCLI:
    def test_verify_chain_valid(self, tmp_path, capsys):
        db = str(tmp_path / "decisions.db")
        store = SQLiteDecisionRecordStore(db)
        a1 = _make_assessment(timestamp=NOW)
        a2 = _make_assessment(previous_hash=a1.hash, timestamp=T1, payload={"current_value": 999})
        store.put_assessment(a1)
        store.put_assessment(a2)

        exit_code = main(["verify-records", "--decision-store", db, "--chain", "assessments", "--stream", "sli:checkout:latency-p99"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "VERIFIED" in captured.out

    def test_verify_chain_empty(self, tmp_path, capsys):
        db = str(tmp_path / "decisions.db")
        SQLiteDecisionRecordStore(db)  # create schema

        exit_code = main(["verify-records", "--decision-store", db, "--chain", "assessments", "--stream", "nonexistent"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "VERIFIED" in captured.out
        assert "Records: 0" in captured.out

    def test_verify_incident_valid(self, tmp_path, capsys):
        db = str(tmp_path / "decisions.db")
        store = SQLiteDecisionRecordStore(db)

        # Build a complete incident with all record types
        a = _make_assessment(incident_id="inc-001")
        store.put_assessment(a)

        v = _make_verdict(incident_id="inc-001", input_hashes=[a.hash])
        store.put_verdict(v)
        store.put_prompt(v.prompt_hash, "prompt text")
        store.put_response(v.response_hash, "response text")

        e = _make_evaluation(incident_id="inc-001", verdict_hash=v.hash, evidence_hashes=[a.hash])
        store.put_evaluation(e)

        inc = Incident(id="inc-001", created_at=NOW, trigger_hash=a.hash, stream="sli:checkout:latency-p99")
        store.create_incident(inc)

        exit_code = main(["verify-records", "--decision-store", db, "--incident", "inc-001"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "VERIFIED" in captured.out

    def test_verify_incident_not_found(self, tmp_path, capsys):
        db = str(tmp_path / "decisions.db")
        SQLiteDecisionRecordStore(db)

        exit_code = main(["verify-records", "--decision-store", db, "--incident", "nonexistent"])
        assert exit_code == 1

    def test_verify_chain_evaluations(self, tmp_path, capsys):
        db = str(tmp_path / "decisions.db")
        store = SQLiteDecisionRecordStore(db)
        e = _make_evaluation(incident_id="inc-eval-test")
        store.put_evaluation(e)

        exit_code = main(["verify-records", "--decision-store", db,
                          "--chain", "evaluations", "--incident-id", "inc-eval-test"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "VERIFIED" in captured.out
        assert "Records: 1" in captured.out

    def test_missing_key_returns_exit_2(self, tmp_path, capsys):
        db = str(tmp_path / "decisions.db")
        SQLiteDecisionRecordStore(db)

        exit_code = main(["verify-records", "--decision-store", db,
                          "--chain", "assessments"])
        assert exit_code == 2

    def test_verify_help(self, capsys):
        try:
            main(["verify-records", "--help"])
        except SystemExit:
            pass
        captured = capsys.readouterr()
        assert "verify" in captured.out


class TestFullChainIntegration:
    """End-to-end: assessment → incident → verdict → evaluation → verify."""

    def test_full_chain_verifiable(self, tmp_path, capsys):
        db = str(tmp_path / "decisions.db")
        store = SQLiteDecisionRecordStore(db)

        # 1. Observe writes breach assessment
        a = _make_assessment(incident_id="inc-full", stream="sli:fraud-detect:reversal_rate")
        store.put_assessment(a)

        # 2. Observe creates incident envelope
        inc = Incident(id="inc-full", created_at=NOW, trigger_hash=a.hash, stream="sli:fraud-detect:reversal_rate")
        store.create_incident(inc)

        # 3. Correlate writes verdict
        v_corr = _make_verdict(agent="correlate", incident_id="inc-full",
                               input_hashes=[a.hash], timestamp=T1, reasoning="correlated")
        store.put_verdict(v_corr)
        store.put_prompt(v_corr.prompt_hash, "correlate prompt")
        store.put_response(v_corr.response_hash, "correlate response")

        # 4. Respond writes triage verdict
        v_triage = _make_verdict(agent="triage", incident_id="inc-full",
                                 previous_hash=ZERO_HASH, timestamp=T2,
                                 input_hashes=[v_corr.hash], reasoning="triaged",
                                 prompt_hash="f" * 64, response_hash="a" * 64)
        store.put_verdict(v_triage)
        store.put_prompt(v_triage.prompt_hash, "triage prompt")
        store.put_response(v_triage.response_hash, "triage response")

        # 5. Learn writes evaluation
        ev = _make_evaluation(incident_id="inc-full", verdict_hash=v_triage.hash,
                              evidence_hashes=[a.hash], timestamp=T3)
        store.put_evaluation(ev)

        # 6. Verify the full incident
        exit_code = main(["verify-records", "--decision-store", db, "--incident", "inc-full"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "VERIFIED" in captured.out
        assert "1" in captured.out  # at least 1 assessment
