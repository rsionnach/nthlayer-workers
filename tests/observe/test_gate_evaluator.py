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

    def test_slo_without_percent_consumed_ignored(self):
        store = _make_store_with_assessments("svc", [
            {"slo_name": "avail", "status": "NO_DATA"},  # no percent_consumed
        ])
        result = check_deploy("svc", "standard", store)
        assert result.result == GateResult.APPROVED
        assert result.budget_remaining_pct == 100.0


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
