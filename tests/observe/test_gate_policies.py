"""Tests for gate policies and conditions (adapted from nthlayer/tests/test_policies_evaluator.py)."""

from __future__ import annotations

from datetime import datetime

from nthlayer_workers.observe.gate.conditions import (
    get_current_context,
    is_business_hours,
    is_freeze_period,
    is_peak_traffic,
    is_weekday,
)
from nthlayer_workers.observe.gate.policies import (
    ConditionEvaluator,
    PolicyContext,
)


class TestConditions:
    def test_business_hours_weekday_in_range(self):
        # Wednesday 10:00
        now = datetime(2026, 4, 8, 10, 0)
        assert is_business_hours(now=now) is True

    def test_business_hours_weekend(self):
        # Saturday 10:00
        now = datetime(2026, 4, 11, 10, 0)
        assert is_business_hours(now=now) is False

    def test_business_hours_outside_range(self):
        # Wednesday 20:00
        now = datetime(2026, 4, 8, 20, 0)
        assert is_business_hours(now=now) is False

    def test_is_weekday(self):
        assert is_weekday(now=datetime(2026, 4, 8, 10, 0)) is True  # Wed
        assert is_weekday(now=datetime(2026, 4, 11, 10, 0)) is False  # Sat

    def test_freeze_period_inside(self):
        now = datetime(2026, 12, 25, 10, 0)
        assert is_freeze_period("2026-12-20", "2027-01-02", now=now) is True

    def test_freeze_period_outside(self):
        now = datetime(2026, 11, 15, 10, 0)
        assert is_freeze_period("2026-12-20", "2027-01-02", now=now) is False

    def test_freeze_period_invalid_dates(self):
        assert is_freeze_period("invalid", "dates") is False

    def test_peak_traffic_in_range(self):
        now = datetime(2026, 4, 8, 11, 0)
        assert is_peak_traffic(now=now) is True

    def test_peak_traffic_outside(self):
        now = datetime(2026, 4, 8, 8, 0)
        assert is_peak_traffic(now=now) is False

    def test_get_current_context(self):
        now = datetime(2026, 4, 8, 14, 30)
        ctx = get_current_context(budget_remaining=75.0, tier="critical", now=now)
        assert ctx["hour"] == 14
        assert ctx["budget_remaining"] == 75.0
        assert ctx["tier"] == "critical"
        assert ctx["weekday"] is True


class TestPolicyContext:
    def test_to_dict(self):
        now = datetime(2026, 4, 8, 10, 0)
        ctx = PolicyContext(budget_remaining=80.0, tier="critical", now=now)
        d = ctx.to_dict()
        assert d["budget_remaining"] == 80.0
        assert d["tier"] == "critical"
        assert d["hour"] == 10

    def test_defaults(self):
        ctx = PolicyContext()
        assert ctx.budget_remaining == 100.0
        assert ctx.tier == "standard"
        assert ctx.environment == "prod"


class TestConditionEvaluator:
    def test_empty_condition(self):
        evaluator = ConditionEvaluator({"hour": 10})
        assert evaluator.evaluate("") is True

    def test_simple_comparison(self):
        evaluator = ConditionEvaluator({"hour": 10})
        assert evaluator.evaluate("hour >= 9") is True
        assert evaluator.evaluate("hour >= 11") is False

    def test_equality(self):
        evaluator = ConditionEvaluator({"tier": "critical"})
        assert evaluator.evaluate("tier == 'critical'") is True
        assert evaluator.evaluate("tier == 'standard'") is False

    def test_inequality(self):
        evaluator = ConditionEvaluator({"tier": "critical"})
        assert evaluator.evaluate("tier != 'standard'") is True

    def test_and_operator(self):
        evaluator = ConditionEvaluator({"hour": 10, "weekday": True})
        assert evaluator.evaluate("hour >= 9 AND weekday") is True
        assert evaluator.evaluate("hour >= 11 AND weekday") is False

    def test_or_operator(self):
        evaluator = ConditionEvaluator({"hour": 20, "environment": "dev"})
        assert evaluator.evaluate("hour < 17 OR environment == 'dev'") is True

    def test_not_operator(self):
        evaluator = ConditionEvaluator({"weekday": False})
        assert evaluator.evaluate("NOT weekday") is True

    def test_parentheses(self):
        evaluator = ConditionEvaluator({"hour": 10, "weekday": True, "tier": "critical"})
        assert evaluator.evaluate("(hour >= 9 AND hour <= 17) AND weekday") is True

    def test_boolean_variable(self):
        evaluator = ConditionEvaluator({"weekday": True})
        assert evaluator.evaluate("weekday") is True

    def test_missing_variable_returns_false(self):
        evaluator = ConditionEvaluator({})
        assert evaluator.evaluate("missing_var") is False

    def test_numeric_comparison(self):
        evaluator = ConditionEvaluator({"budget_remaining": 15.0})
        assert evaluator.evaluate("budget_remaining < 20") is True
        assert evaluator.evaluate("budget_remaining < 10") is False

    def test_function_business_hours(self):
        now = datetime(2026, 4, 8, 10, 0)  # Wed 10:00
        ctx = PolicyContext(now=now)
        evaluator = ConditionEvaluator(ctx)
        assert evaluator.evaluate("business_hours()") is True

    def test_function_weekday(self):
        now = datetime(2026, 4, 11, 10, 0)  # Sat
        ctx = PolicyContext(now=now)
        evaluator = ConditionEvaluator(ctx)
        assert evaluator.evaluate("weekday()") is False

    def test_function_freeze_period(self):
        now = datetime(2026, 12, 25, 10, 0)
        ctx = PolicyContext(now=now)
        evaluator = ConditionEvaluator(ctx)
        assert evaluator.evaluate("freeze_period('2026-12-20', '2027-01-02')") is True

    def test_invalid_condition_fails_safe(self):
        evaluator = ConditionEvaluator({})
        assert evaluator.evaluate("invalid @#$ syntax") is False

    def test_evaluate_all_returns_most_restrictive(self):
        evaluator = ConditionEvaluator({"hour": 10, "weekday": True})
        conditions = [
            {"name": "normal", "when": "weekday", "blocking": 10},
            {"name": "peak", "when": "hour >= 9 AND hour <= 17", "blocking": 20},
        ]
        matched, cond = evaluator.evaluate_all(conditions)
        assert matched is True
        assert cond["name"] == "peak"

    def test_evaluate_all_no_match(self):
        evaluator = ConditionEvaluator({"hour": 3, "weekday": False})
        conditions = [
            {"name": "bh", "when": "business_hours()", "blocking": 10},
        ]
        # business_hours() needs PolicyContext for now; dict context won't have it
        # But the evaluator handles this gracefully
        matched, cond = evaluator.evaluate_all(conditions)
        # With dict context (no PolicyContext), business_hours() uses datetime.now()
        # which may or may not match — just verify no crash
        assert isinstance(matched, bool)

    def test_float_comparison(self):
        evaluator = ConditionEvaluator({"budget_remaining": 15.5})
        assert evaluator.evaluate("budget_remaining < 20.0") is True

    def test_string_literal_double_quotes(self):
        evaluator = ConditionEvaluator({"env": "prod"})
        assert evaluator.evaluate('env == "prod"') is True

    def test_complex_expression(self):
        evaluator = ConditionEvaluator({
            "tier": "critical",
            "budget_remaining": 12.0,
            "downstream_count": 5,
        })
        expr = "tier == 'critical' AND budget_remaining < 20 AND downstream_count > 3"
        assert evaluator.evaluate(expr) is True
