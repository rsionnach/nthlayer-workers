"""Tests for incident envelope creation."""

import argparse

from nthlayer_common.records.models import IncidentStatus
from nthlayer_common.records.sqlite_store import SQLiteDecisionRecordStore
from nthlayer_workers.observe.assessment import create as create_assessment
from nthlayer_workers.observe.cli import _write_decision_record
from nthlayer_workers.observe.incident import create_incident_from_breach


class TestCreateIncidentFromBreach:
    def test_creates_incident_envelope(self, tmp_path):
        db = str(tmp_path / "decisions.db")
        store = SQLiteDecisionRecordStore(db)

        # First write a breach assessment
        args = argparse.Namespace(decision_store=db, legacy_store=True)
        legacy = create_assessment("slo_status", "checkout", {
            "slo_name": "availability", "status": "CRITICAL",
            "objective": 99.9, "current_sli": 99.85,
            "percent_consumed": 89.8, "burned_minutes": 38.8,
            "total_budget_minutes": 43.2, "window": "30d", "error": None,
        })
        _write_decision_record(args, legacy)

        # Get the assessment hash
        chain = store.get_chain("assessment", "sli:checkout:availability")
        assert len(chain) == 1
        trigger_hash = chain[0].hash

        # Create incident from breach
        incident = create_incident_from_breach(store, trigger_hash, "sli:checkout:availability")
        assert incident.trigger_hash == trigger_hash
        assert incident.stream == "sli:checkout:availability"
        assert incident.status == IncidentStatus.OPEN
        assert incident.id.startswith("inc-")

    def test_incident_retrievable(self, tmp_path):
        db = str(tmp_path / "decisions.db")
        store = SQLiteDecisionRecordStore(db)

        args = argparse.Namespace(decision_store=db, legacy_store=True)
        legacy = create_assessment("slo_status", "checkout", {
            "slo_name": "availability", "status": "EXHAUSTED",
            "objective": 99.9, "current_sli": 99.7,
            "percent_consumed": 100.0, "burned_minutes": 43.2,
            "total_budget_minutes": 43.2, "window": "30d", "error": None,
        })
        _write_decision_record(args, legacy)

        chain = store.get_chain("assessment", "sli:checkout:availability")
        trigger_hash = chain[0].hash

        incident = create_incident_from_breach(store, trigger_hash, "sli:checkout:availability")
        retrieved = store.get_incident(incident.id)
        assert retrieved is not None
        assert retrieved.id == incident.id
        assert retrieved.trigger_hash == trigger_hash

    def test_incident_id_format(self, tmp_path):
        db = str(tmp_path / "decisions.db")
        store = SQLiteDecisionRecordStore(db)

        args = argparse.Namespace(decision_store=db, legacy_store=True)
        legacy = create_assessment("slo_status", "checkout", {
            "slo_name": "availability", "status": "CRITICAL",
            "objective": 99.9, "current_sli": 99.85,
            "percent_consumed": 89.8, "burned_minutes": 38.8,
            "total_budget_minutes": 43.2, "window": "30d", "error": None,
        })
        _write_decision_record(args, legacy)

        chain = store.get_chain("assessment", "sli:checkout:availability")
        incident = create_incident_from_breach(store, chain[0].hash, "sli:checkout:availability")
        # Format: inc-{uuid}
        assert incident.id.startswith("inc-")
        assert len(incident.id) > 4
