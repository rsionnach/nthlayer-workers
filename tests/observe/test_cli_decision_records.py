"""Tests for CLI integration with decision record store (dual-write)."""

import argparse


from nthlayer_common.records.models import ZERO_HASH
from nthlayer_common.records.sqlite_store import SQLiteDecisionRecordStore
from nthlayer_common.records.hashing import verify_hash
from nthlayer_workers.observe.assessment import create as create_assessment
from nthlayer_workers.observe.cli import _write_decision_record


class TestWriteDecisionRecord:
    def test_writes_when_decision_store_configured(self, tmp_path):
        db = str(tmp_path / "decisions.db")
        args = argparse.Namespace(decision_store=db, legacy_store=True)
        legacy = create_assessment("slo_status", "checkout", {
            "slo_name": "availability", "status": "CRITICAL",
            "objective": 99.9, "current_sli": 99.85,
            "percent_consumed": 89.8, "burned_minutes": 38.8,
            "total_budget_minutes": 43.2, "window": "30d", "error": None,
        })

        _write_decision_record(args, legacy)

        store = SQLiteDecisionRecordStore(db)
        chain = store.get_chain("assessment", "sli:checkout:availability")
        assert len(chain) == 1
        assert verify_hash(chain[0]) is True
        assert chain[0].previous_hash == ZERO_HASH

    def test_skips_when_no_decision_store(self, tmp_path):
        args = argparse.Namespace(decision_store=None, legacy_store=True)
        legacy = create_assessment("slo_status", "checkout", {
            "slo_name": "availability", "status": "HEALTHY",
            "objective": 99.9, "current_sli": 99.95,
            "percent_consumed": 20.0, "burned_minutes": 8.6,
            "total_budget_minutes": 43.2, "window": "30d", "error": None,
        })
        # Should not raise
        _write_decision_record(args, legacy)

    def test_chains_multiple_records(self, tmp_path):
        db = str(tmp_path / "decisions.db")
        args = argparse.Namespace(decision_store=db, legacy_store=True)

        a1 = create_assessment("slo_status", "checkout", {
            "slo_name": "availability", "status": "HEALTHY",
            "objective": 99.9, "current_sli": 99.95,
            "percent_consumed": 20.0, "burned_minutes": 8.6,
            "total_budget_minutes": 43.2, "window": "30d", "error": None,
        })
        a2 = create_assessment("slo_status", "checkout", {
            "slo_name": "availability", "status": "CRITICAL",
            "objective": 99.9, "current_sli": 99.85,
            "percent_consumed": 89.8, "burned_minutes": 38.8,
            "total_budget_minutes": 43.2, "window": "30d", "error": None,
        })

        _write_decision_record(args, a1)
        _write_decision_record(args, a2)

        store = SQLiteDecisionRecordStore(db)
        chain = store.get_chain("assessment", "sli:checkout:availability")
        assert len(chain) == 2
        assert chain[0].previous_hash == ZERO_HASH
        assert chain[1].previous_hash == chain[0].hash

    def test_different_streams_independent(self, tmp_path):
        db = str(tmp_path / "decisions.db")
        args = argparse.Namespace(decision_store=db, legacy_store=True)

        a1 = create_assessment("slo_status", "checkout", {
            "slo_name": "availability", "status": "HEALTHY",
            "objective": 99.9, "current_sli": 99.95,
            "percent_consumed": 20.0, "burned_minutes": 8.6,
            "total_budget_minutes": 43.2, "window": "30d", "error": None,
        })
        a2 = create_assessment("slo_status", "checkout", {
            "slo_name": "latency", "status": "WARNING",
            "objective": 500, "current_sli": 480.0,
            "percent_consumed": 60.0, "burned_minutes": 25.9,
            "total_budget_minutes": 43.2, "window": "30d", "error": None,
        })

        _write_decision_record(args, a1)
        _write_decision_record(args, a2)

        store = SQLiteDecisionRecordStore(db)
        avail_chain = store.get_chain("assessment", "sli:checkout:availability")
        latency_chain = store.get_chain("assessment", "sli:checkout:latency")
        assert len(avail_chain) == 1
        assert len(latency_chain) == 1
        # Both are genesis records
        assert avail_chain[0].previous_hash == ZERO_HASH
        assert latency_chain[0].previous_hash == ZERO_HASH

    def test_gate_assessment_stored(self, tmp_path):
        db = str(tmp_path / "decisions.db")
        args = argparse.Namespace(decision_store=db, legacy_store=True)

        legacy = create_assessment("deploy_gate", "checkout", {
            "action": "deploy", "decision": "blocked",
            "budget_remaining_pct": 8.5, "warning_threshold": 20.0,
            "blocking_threshold": 10.0, "slo_count": 3,
            "reasons": ["Budget below threshold"],
        })

        _write_decision_record(args, legacy)

        store = SQLiteDecisionRecordStore(db)
        chain = store.get_chain("assessment", "deploy_gate:checkout")
        assert len(chain) == 1
        assert chain[0].severity.value == "critical"
