"""Tests for the deployment correlator (adapted from nthlayer/tests/test_slo_correlator.py)."""

from __future__ import annotations

from datetime import datetime

from nthlayer_workers.observe.gate.correlator import (
    BLOCKING_CONFIDENCE,
    HIGH_CONFIDENCE,
    LOW_CONFIDENCE,
    MEDIUM_CONFIDENCE,
    CorrelationInput,
    CorrelationResult,
    correlate,
    _calculate_burn_rate_score,
    _calculate_dependency_score,
    _calculate_history_score,
    _calculate_magnitude_score,
    _calculate_proximity_score,
)


def _make_input(**overrides) -> CorrelationInput:
    """Helper to create a CorrelationInput with defaults."""
    defaults = {
        "deployment_id": "deploy-001",
        "service": "payment-api",
        "deploy_time": datetime(2026, 4, 8, 14, 0),
        "burn_detected_at": datetime(2026, 4, 8, 14, 15),  # 15 min after deploy
        "burn_rate_before": 0.01,
        "burn_rate_after": 0.05,
        "burn_minutes": 6.0,
        "is_same_service": True,
    }
    defaults.update(overrides)
    return CorrelationInput(**defaults)


class TestBurnRateScore:
    def test_spike_5x(self):
        score = _calculate_burn_rate_score(0.01, 0.05)
        assert score == 1.0

    def test_spike_2x(self):
        score = _calculate_burn_rate_score(0.01, 0.02)
        assert 0.3 < score < 0.5

    def test_no_baseline(self):
        score = _calculate_burn_rate_score(0.0, 0.05)
        assert score == 0.5

    def test_no_change(self):
        score = _calculate_burn_rate_score(0.01, 0.01)
        assert score == 0.2  # 1x / 5 = 0.2

    def test_no_burn(self):
        score = _calculate_burn_rate_score(0.0, 0.0)
        assert score == 0.0


class TestProximityScore:
    def test_zero_elapsed(self):
        now = datetime(2026, 4, 8, 14, 0)
        score = _calculate_proximity_score(now, now)
        assert score == 1.0

    def test_30_min_elapsed(self):
        from datetime import timedelta
        t1 = datetime(2026, 4, 8, 14, 0)
        t2 = t1 + timedelta(minutes=30)
        score = _calculate_proximity_score(t1, t2)
        assert 0.35 < score < 0.40  # exp(-1) ≈ 0.368


class TestMagnitudeScore:
    def test_ten_minutes(self):
        assert _calculate_magnitude_score(10.0) == 1.0

    def test_five_minutes(self):
        assert _calculate_magnitude_score(5.0) == 0.5

    def test_zero(self):
        assert _calculate_magnitude_score(0.0) == 0.0

    def test_capped_at_one(self):
        assert _calculate_magnitude_score(20.0) == 1.0


class TestDependencyScore:
    def test_same_service(self):
        inp = _make_input(is_same_service=True)
        assert _calculate_dependency_score(inp) == 1.0

    def test_direct_upstream(self):
        inp = _make_input(is_same_service=False, is_direct_upstream=True)
        assert _calculate_dependency_score(inp) == 1.0

    def test_transitive(self):
        inp = _make_input(is_same_service=False, is_transitive_upstream=True)
        assert _calculate_dependency_score(inp) == 0.4

    def test_yaml_downstream(self):
        inp = _make_input(is_same_service=False, is_yaml_downstream=True)
        assert _calculate_dependency_score(inp) == 0.6

    def test_no_relationship(self):
        inp = _make_input(is_same_service=False)
        assert _calculate_dependency_score(inp) == 0.0


class TestHistoryScore:
    def test_no_deploys(self):
        assert _calculate_history_score(0, 0) == 0.0

    def test_half_correlated(self):
        assert _calculate_history_score(10, 5) == 0.5

    def test_all_correlated(self):
        assert _calculate_history_score(10, 10) == 1.0

    def test_capped_at_one(self):
        assert _calculate_history_score(5, 10) == 1.0


class TestCorrelate:
    def test_high_confidence_scenario(self):
        inp = _make_input(
            burn_rate_before=0.01,
            burn_rate_after=0.10,  # 10x spike
            burn_minutes=15.0,
            is_same_service=True,
            recent_deploy_count=5,
            prior_correlations=3,
        )
        result = correlate(inp)
        assert result.confidence >= HIGH_CONFIDENCE
        assert result.confidence_label == "HIGH"

    def test_low_confidence_scenario(self):
        inp = _make_input(
            burn_rate_before=0.01,
            burn_rate_after=0.01,  # no spike
            burn_minutes=1.0,  # minimal burn
            is_same_service=False,
            recent_deploy_count=10,
            prior_correlations=0,
        )
        result = correlate(inp)
        assert result.confidence < MEDIUM_CONFIDENCE

    def test_result_fields(self):
        inp = _make_input()
        result = correlate(inp)
        assert result.deployment_id == "deploy-001"
        assert result.service == "payment-api"
        assert result.method == "time_window_analysis"
        assert "burn_rate_score" in result.details
        assert "proximity_score" in result.details
        assert "magnitude_score" in result.details
        assert "dependency_score" in result.details
        assert "history_score" in result.details

    def test_confidence_labels(self):
        assert CorrelationResult("d", "s", 0, 0.8).confidence_label == "HIGH"
        assert CorrelationResult("d", "s", 0, 0.6).confidence_label == "MEDIUM"
        assert CorrelationResult("d", "s", 0, 0.4).confidence_label == "LOW"
        assert CorrelationResult("d", "s", 0, 0.1).confidence_label == "NONE"

    def test_thresholds_are_correct(self):
        assert HIGH_CONFIDENCE == 0.7
        assert MEDIUM_CONFIDENCE == 0.5
        assert LOW_CONFIDENCE == 0.3
        assert BLOCKING_CONFIDENCE == 0.8
