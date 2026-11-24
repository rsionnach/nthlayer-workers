"""Tests for the deployment gate evaluator."""

from __future__ import annotations

import pytest
from nthlayer_common.gate_models import GatePolicy, GateResult

from nthlayer_workers.observe.assessment import create
from nthlayer_workers.observe.gate.evaluator import check_deploy
from nthlayer_workers.observe.store import MemoryAssessmentStore


def _make_store_with_assessments(service: str, assessments: list[dict]) -> MemoryAssessmentStore:
    """Create a store pre-populated with slo_status assessments."""
    store = MemoryAssessmentStore()
    for data in assessments:
        store.put(create("slo_status", service, data))
    return store


class TestCheckDeploy:
    def test_approved_healthy_budget(self):
        store = _make_store_with_assessments("svc", [
            {"slo_name": "availability", "status": "HEALTHY", "percent_consumed": 20.0},
        ])
        result = check_deploy("svc", "standard", store)
        assert result.result == GateResult.APPROVED
        assert result.budget_remaining_pct == 80.0

    def test_warning_low_budget(self):
        store = _make_store_with_assessments("svc", [
            {"slo_name": "availability", "status": "WARNING", "percent_consumed": 85.0},
        ])
        result = check_deploy("svc", "standard", store)
        assert result.result == GateResult.WARNING
        assert result.budget_remaining_pct == 15.0

    def test_blocked_critical_tier(self):
        store = _make_store_with_assessments("svc", [
            {"slo_name": "availability", "status": "CRITICAL", "percent_consumed": 92.0},
        ])
        result = check_deploy("svc", "critical", store)
        assert result.result == GateResult.BLOCKED
        assert result.budget_remaining_pct == 8.0

    def test_approved_no_assessments(self):
        store = MemoryAssessmentStore()
        result = check_deploy("svc", "standard", store)
        assert result.result == GateResult.APPROVED
        assert result.slo_count == 0

    def test_multiple_slos_averaged(self):
        store = _make_store_with_assessments("svc", [
            {"slo_name": "availability", "percent_consumed": 30.0},
            {"slo_name": "latency", "percent_consumed": 70.0},
        ])
        result = check_deploy("svc", "standard", store)
        # Average: 50%, remaining: 50% — above warning threshold (20%)
        assert result.budget_remaining_pct == 50.0
        assert result.slo_count == 2

    def test_custom_policy_warning_threshold(self):
        store = _make_store_with_assessments("svc", [
            {"slo_name": "avail", "percent_consumed": 60.0},
        ])
        policy = GatePolicy(warning=50.0)
        result = check_deploy("svc", "standard", store, policy=policy)
        # 40% remaining, warning at 50% → WARNING
        assert result.result == GateResult.WARNING

    def test_custom_policy_blocking_threshold(self):
        store = _make_store_with_assessments("svc", [
            {"slo_name": "avail", "percent_consumed": 80.0},
        ])
        policy = GatePolicy(warning=30.0, blocking=25.0)
        result = check_deploy("svc", "standard", store, policy=policy)
        # 20% remaining, blocking at 25% → BLOCKED
        assert result.result == GateResult.BLOCKED

    def test_low_tier_advisory_only(self):
        store = _make_store_with_assessments("svc", [
            {"slo_name": "avail", "percent_consumed": 92.0},
        ])
        # Low tier has blocking=None (advisory only)
        result = check_deploy("svc", "low", store)
        # 8% remaining, no blocking threshold → WARNING (not BLOCKED)
        assert result.result == GateResult.WARNING

    def test_exhaustion_with_freeze_policy(self):
        store = _make_store_with_assessments("svc", [
            {"slo_name": "avail", "percent_consumed": 105.0},
        ])
        policy = GatePolicy(on_exhausted=["freeze_deploys"])
        result = check_deploy("svc", "critical", store, policy=policy)
        assert result.result == GateResult.BLOCKED
        assert "frozen" in result.message.lower()

    def test_exhaustion_with_require_approval(self):
        store = _make_store_with_assessments("svc", [
            {"slo_name": "avail", "percent_consumed": 105.0},
        ])
        policy = GatePolicy(on_exhausted=["require_approval"])
        result = check_deploy("svc", "critical", store, policy=policy)
        assert result.result == GateResult.WARNING
        assert "approval" in result.message.lower()

    def test_result_has_recommendations(self):
        store = _make_store_with_assessments("svc", [
            {"slo_name": "avail", "percent_consumed": 92.0},
        ])
        result = check_deploy("svc", "critical", store)
        assert result.result == GateResult.BLOCKED
        assert len(result.recommendations) > 0

    def test_no_reading_with_a_budget_is_a_warning_not_a_healthy_budget(self):
        """Readings exist and none states a budget: that is not 100% left
        [opensrm-9qa6]."""
        store = _make_store_with_assessments("svc", [
            {"slo_name": "avail", "status": "NO_DATA", "error": "no error budget series"},
            {"slo_name": "latency", "status": "ERROR", "error": "timeout"},
            {"slo_name": "lag", "status": "NO_DATA", "error": "not_evaluable: x"},
        ])
        result = check_deploy("svc", "critical", store)
        assert result.result == GateResult.WARNING
        assert result.budget_remaining_pct is None
        assert result.slo_count == 3
        assert result.message == "No budget data for 3 SLOs"

    def test_a_missing_budget_points_at_the_recording_rules(self):
        """One SLO that could have a budget and has none: the rules may not
        be loaded."""
        store = _make_store_with_assessments("svc", [
            {"slo_name": "avail", "status": "NO_DATA", "error": "no error budget series"},
            {"slo_name": "lag", "status": "NO_DATA",
             "error": "not_evaluable: no error budget is written for slo_type 'custom'"},
        ])
        result = check_deploy("svc", "standard", store)
        assert result.result == GateResult.WARNING
        assert result.recommendations == [
            "Check that nthlayer-generate's recording rules are loaded in Prometheus",
        ]

    def test_a_service_with_no_budgetable_slo_is_not_told_to_load_the_rules(self):
        """Every SLO is one no budget is ever written for: loading the rules
        would change nothing, so the gate does not suggest it."""
        store = _make_store_with_assessments("svc", [
            {"slo_name": "reversal_rate", "status": "NO_DATA",
             "error": "not_evaluable: judgment SLOs are evaluated by measure"},
            {"slo_name": "lag", "status": "NO_DATA",
             "error": "not_evaluable: no error budget is written for slo_type 'custom'"},
        ])
        result = check_deploy("svc", "standard", store)
        assert result.result == GateResult.WARNING
        assert result.message == "No budget data for 2 SLOs"
        assert result.recommendations == [
            "None of this service's SLOs has an error budget to read "
            "(judgment SLOs, or kinds no budget is written for)",
        ]

    def test_one_slo_without_a_budget_is_named_in_the_singular(self):
        store = _make_store_with_assessments("svc", [
            {"slo_name": "avail", "status": "NO_DATA"},
        ])
        result = check_deploy("svc", "standard", store)
        assert result.result == GateResult.WARNING
        assert result.message == "No budget data for 1 SLO"

    def test_a_reading_with_a_budget_decides_beside_ones_without(self):
        """The readings without a budget are left out of the average, as
        before; one reading with a budget is enough to decide."""
        store = _make_store_with_assessments("svc", [
            {"slo_name": "avail", "status": "HEALTHY", "percent_consumed": 20.0},
            {"slo_name": "lag", "status": "NO_DATA"},
        ])
        result = check_deploy("svc", "standard", store)
        assert result.result == GateResult.APPROVED
        assert result.budget_remaining_pct == 80.0
        assert result.slo_count == 2

    def test_a_fully_spent_budget_still_blocks_beside_ones_without(self):
        store = _make_store_with_assessments("svc", [
            {"slo_name": "avail", "status": "EXHAUSTED", "percent_consumed": 130.0},
            {"slo_name": "lag", "status": "NO_DATA"},
        ])
        result = check_deploy("svc", "critical", store)
        assert result.result == GateResult.BLOCKED
        assert result.budget_remaining_pct == 0.0


class TestCheckDeployCLI:
    def test_check_deploy_help(self, capsys):
        from nthlayer_workers.observe.cli import main

        with pytest.raises(SystemExit) as exc_info:
            main(["check-deploy", "--help"])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "--service" in captured.out
        assert "--tier" in captured.out
        assert "--store" in captured.out

    def test_check_deploy_with_no_budget_data_warns_and_states_no_budget(self, tmp_path, capsys):
        """Through the legacy CLI and a real SQLite store: exit 1 (WARNING),
        a null budget in the output and in the stored deploy_gate reading,
        and a decision record that says the budget is unknown [opensrm-9qa6]."""
        import json

        from nthlayer_workers.observe.cli import main
        from nthlayer_workers.observe.decision_records import generate_summaries
        from nthlayer_workers.observe.sqlite_store import SQLiteAssessmentStore
        from nthlayer_workers.observe.store import AssessmentFilter

        db = tmp_path / "assessments.db"
        with SQLiteAssessmentStore(str(db)) as store:
            store.put(create("slo_status", "svc", {
                "slo_name": "availability", "status": "NO_DATA",
                "error": "no error budget series",
            }))

        exit_code = main(["check-deploy", "--service", "svc", "--tier", "critical", "--store", str(db)])

        assert exit_code == 1
        printed = json.loads(capsys.readouterr().out)
        assert (printed["decision"], printed["budget_remaining_pct"]) == ("WARNING", None)
        with SQLiteAssessmentStore(str(db)) as store:
            (gate,) = store.query(AssessmentFilter(service="svc", kind="deploy_gate"))
        assert gate.data["decision"] == "warning"
        assert gate.data["budget_remaining_pct"] is None
        assert generate_summaries(gate).technical == (
            "svc deploy gate: WARNING. Budget remaining: unknown. No budget data for 1 SLO"
        )
