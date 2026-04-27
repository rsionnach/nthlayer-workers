"""Tests for the drift detection module (adapted from nthlayer/tests/test_drift.py)."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from nthlayer_workers.observe.drift import (
    DRIFT_DEFAULTS,
    DriftAnalysisError,
    DriftAnalyzer,
    DriftMetrics,
    DriftPattern,
    DriftProjection,
    DriftResult,
    DriftSeverity,
    PatternDetector,
    get_drift_defaults,
)


class TestDriftModels:
    def test_drift_severity_values(self):
        assert DriftSeverity.NONE.value == "none"
        assert DriftSeverity.INFO.value == "info"
        assert DriftSeverity.WARN.value == "warn"
        assert DriftSeverity.CRITICAL.value == "critical"

    def test_drift_pattern_values(self):
        assert DriftPattern.STABLE.value == "stable"
        assert DriftPattern.GRADUAL_DECLINE.value == "gradual_decline"
        assert DriftPattern.GRADUAL_IMPROVEMENT.value == "gradual_improvement"
        assert DriftPattern.STEP_CHANGE_DOWN.value == "step_change_down"
        assert DriftPattern.STEP_CHANGE_UP.value == "step_change_up"
        assert DriftPattern.VOLATILE.value == "volatile"

    def test_drift_metrics_creation(self):
        metrics = DriftMetrics(
            slope_per_day=-0.001,
            slope_per_week=-0.007,
            r_squared=0.85,
            current_budget=0.72,
            budget_at_window_start=0.80,
            variance=0.001,
            data_points=720,
        )
        assert metrics.slope_per_day == -0.001
        assert metrics.slope_per_week == -0.007
        assert metrics.r_squared == 0.85
        assert metrics.current_budget == 0.72
        assert metrics.data_points == 720

    def test_drift_projection_creation(self):
        projection = DriftProjection(
            days_until_exhaustion=138,
            projected_budget_30d=0.70,
            projected_budget_60d=0.68,
            projected_budget_90d=0.66,
            confidence=0.85,
        )
        assert projection.days_until_exhaustion == 138
        assert projection.projected_budget_30d == 0.70
        assert projection.confidence == 0.85

    def test_drift_projection_none_exhaustion(self):
        projection = DriftProjection(
            days_until_exhaustion=None,
            projected_budget_30d=0.85,
            projected_budget_60d=0.90,
            projected_budget_90d=0.95,
            confidence=0.90,
        )
        assert projection.days_until_exhaustion is None

    def test_drift_result_to_dict(self):
        now = datetime.now()
        result = DriftResult(
            service_name="test-service",
            tier="critical",
            slo_name="availability",
            window="30d",
            analyzed_at=now,
            data_start=now - timedelta(days=30),
            data_end=now,
            metrics=DriftMetrics(
                slope_per_day=-0.001,
                slope_per_week=-0.007,
                r_squared=0.85,
                current_budget=0.72,
                budget_at_window_start=0.80,
                variance=0.001,
                data_points=720,
            ),
            projection=DriftProjection(
                days_until_exhaustion=100,
                projected_budget_30d=0.70,
                projected_budget_60d=0.68,
                projected_budget_90d=0.66,
                confidence=0.85,
            ),
            pattern=DriftPattern.GRADUAL_DECLINE,
            severity=DriftSeverity.WARN,
            summary="Test summary",
            recommendation="Test recommendation",
            exit_code=1,
        )
        d = result.to_dict()
        assert d["service"] == "test-service"
        assert d["tier"] == "critical"
        assert d["slo"] == "availability"
        assert d["severity"] == "warn"
        assert d["pattern"] == "gradual_decline"
        assert d["exit_code"] == 1
        assert "metrics" in d
        assert "projection" in d

    def test_get_drift_defaults_critical(self):
        defaults = get_drift_defaults("critical")
        assert defaults["enabled"] is True
        assert defaults["window"] == "30d"
        assert "-0.2%/week" in defaults["thresholds"]["warn"]

    def test_get_drift_defaults_standard(self):
        defaults = get_drift_defaults("standard")
        assert defaults["enabled"] is True
        assert defaults["window"] == "30d"
        assert "-0.5%/week" in defaults["thresholds"]["warn"]

    def test_get_drift_defaults_low(self):
        defaults = get_drift_defaults("low")
        assert defaults["enabled"] is False
        assert defaults["window"] == "14d"

    def test_get_drift_defaults_unknown_tier(self):
        defaults = get_drift_defaults("unknown")
        assert defaults == DRIFT_DEFAULTS["standard"]


class TestPatternDetector:
    def test_detect_stable_pattern(self):
        detector = PatternDetector()
        now = datetime.now()
        data = [(now - timedelta(hours=i), 0.80 + (i * 0.00001)) for i in range(100, 0, -1)]
        pattern = detector.detect(data, slope_per_second=0.0000000001, r_squared=0.9)
        assert pattern == DriftPattern.STABLE

    def test_detect_gradual_decline(self):
        detector = PatternDetector()
        now = datetime.now()
        data = [(now - timedelta(days=i), 0.80 - (i * 0.001)) for i in range(30, 0, -1)]
        slope_per_second = -0.001 / 86400
        pattern = detector.detect(data, slope_per_second=slope_per_second, r_squared=0.9)
        assert pattern == DriftPattern.GRADUAL_DECLINE

    def test_detect_gradual_improvement(self):
        detector = PatternDetector()
        now = datetime.now()
        data = [(now - timedelta(days=i), 0.70 + (i * 0.001)) for i in range(30, 0, -1)]
        slope_per_second = 0.001 / 86400
        pattern = detector.detect(data, slope_per_second=slope_per_second, r_squared=0.9)
        assert pattern == DriftPattern.GRADUAL_IMPROVEMENT

    def test_detect_step_change_down(self):
        detector = PatternDetector(step_change_threshold=0.05)
        now = datetime.now()
        data = [
            (now - timedelta(hours=48), 0.90),
            (now - timedelta(hours=36), 0.89),
            (now - timedelta(hours=24), 0.88),
            (now - timedelta(hours=12), 0.78),
            (now, 0.77),
        ]
        pattern = detector.detect(data, slope_per_second=-0.001, r_squared=0.5)
        assert pattern == DriftPattern.STEP_CHANGE_DOWN

    def test_detect_step_change_up(self):
        detector = PatternDetector(step_change_threshold=0.05)
        now = datetime.now()
        data = [
            (now - timedelta(hours=48), 0.70),
            (now - timedelta(hours=36), 0.71),
            (now - timedelta(hours=24), 0.72),
            (now - timedelta(hours=12), 0.82),
            (now, 0.83),
        ]
        pattern = detector.detect(data, slope_per_second=0.001, r_squared=0.5)
        assert pattern == DriftPattern.STEP_CHANGE_UP

    def test_detect_volatile_pattern(self):
        detector = PatternDetector(
            volatility_variance_threshold=0.0005,
            volatility_r_squared_threshold=0.3,
            step_change_threshold=0.2,
        )
        now = datetime.now()
        data = [
            (now - timedelta(hours=i), 0.70 + (0.03 if i % 2 == 0 else -0.03))
            for i in range(50, 0, -1)
        ]
        pattern = detector.detect(data, slope_per_second=0, r_squared=0.1)
        assert pattern == DriftPattern.VOLATILE

    def test_detect_with_insufficient_data(self):
        detector = PatternDetector()
        data = [(datetime.now(), 0.80)]
        pattern = detector.detect(data, slope_per_second=0, r_squared=0)
        assert pattern == DriftPattern.STABLE


class TestDriftAnalyzer:
    def test_analyzer_initialization(self):
        analyzer = DriftAnalyzer(
            prometheus_url="http://prometheus:9090",
            username="user",
            password="pass",
        )
        assert analyzer.prometheus_url == "http://prometheus:9090"
        assert analyzer.username == "user"
        assert analyzer.password == "pass"

    def test_analyzer_url_trailing_slash(self):
        analyzer = DriftAnalyzer(prometheus_url="http://prometheus:9090/")
        assert analyzer.prometheus_url == "http://prometheus:9090"

    def test_parse_threshold(self):
        analyzer = DriftAnalyzer(prometheus_url="http://localhost:9090")
        assert analyzer._parse_threshold("-0.5%/week") == -0.005
        assert analyzer._parse_threshold("-1.0%/week") == -0.01
        assert analyzer._parse_threshold("-0.2%/week") == -0.002

    def test_parse_days(self):
        analyzer = DriftAnalyzer(prometheus_url="http://localhost:9090")
        assert analyzer._parse_days("30d") == 30
        assert analyzer._parse_days("14d") == 14
        assert analyzer._parse_days("7d") == 7

    def test_parse_duration(self):
        analyzer = DriftAnalyzer(prometheus_url="http://localhost:9090")
        assert analyzer._parse_duration("30d") == timedelta(days=30)
        assert analyzer._parse_duration("14d") == timedelta(days=14)
        assert analyzer._parse_duration("1w") == timedelta(weeks=1)
        assert analyzer._parse_duration("24h") == timedelta(hours=24)

    def test_project_exhaustion_declining(self):
        analyzer = DriftAnalyzer(prometheus_url="http://localhost:9090")
        current_budget = 0.72
        slope_per_second = -0.005 / 86400
        days = analyzer._project_exhaustion(current_budget, slope_per_second)
        assert days is not None
        assert 140 <= days <= 150

    def test_project_exhaustion_stable(self):
        analyzer = DriftAnalyzer(prometheus_url="http://localhost:9090")
        days = analyzer._project_exhaustion(0.72, slope_per_second=0.001)
        assert days is None

    def test_project_exhaustion_already_exhausted(self):
        analyzer = DriftAnalyzer(prometheus_url="http://localhost:9090")
        days = analyzer._project_exhaustion(0, slope_per_second=-0.001)
        assert days == 0

    def test_classify_severity_critical_exhaustion(self):
        analyzer = DriftAnalyzer(prometheus_url="http://localhost:9090")
        severity = analyzer._classify_severity(
            slope_per_week=-0.001,
            days_until_exhaustion=5,
            pattern=DriftPattern.GRADUAL_DECLINE,
            thresholds={"warn": "-0.5%/week", "critical": "-1.0%/week"},
            projection_config={"exhaustion_warn": "30d", "exhaustion_critical": "14d"},
        )
        assert severity == DriftSeverity.CRITICAL

    def test_classify_severity_step_change(self):
        analyzer = DriftAnalyzer(prometheus_url="http://localhost:9090")
        severity = analyzer._classify_severity(
            slope_per_week=-0.001,
            days_until_exhaustion=100,
            pattern=DriftPattern.STEP_CHANGE_DOWN,
            thresholds={"warn": "-0.5%/week", "critical": "-1.0%/week"},
            projection_config={"exhaustion_warn": "30d", "exhaustion_critical": "14d"},
        )
        assert severity == DriftSeverity.CRITICAL

    def test_classify_severity_warn(self):
        analyzer = DriftAnalyzer(prometheus_url="http://localhost:9090")
        severity = analyzer._classify_severity(
            slope_per_week=-0.006,
            days_until_exhaustion=50,
            pattern=DriftPattern.GRADUAL_DECLINE,
            thresholds={"warn": "-0.5%/week", "critical": "-1.0%/week"},
            projection_config={"exhaustion_warn": "30d", "exhaustion_critical": "14d"},
        )
        assert severity == DriftSeverity.WARN

    def test_classify_severity_info(self):
        analyzer = DriftAnalyzer(prometheus_url="http://localhost:9090")
        severity = analyzer._classify_severity(
            slope_per_week=-0.001,
            days_until_exhaustion=200,
            pattern=DriftPattern.GRADUAL_DECLINE,
            thresholds={"warn": "-0.5%/week", "critical": "-1.0%/week"},
            projection_config={"exhaustion_warn": "30d", "exhaustion_critical": "14d"},
        )
        assert severity == DriftSeverity.INFO

    def test_classify_severity_none(self):
        analyzer = DriftAnalyzer(prometheus_url="http://localhost:9090")
        severity = analyzer._classify_severity(
            slope_per_week=0.001,
            days_until_exhaustion=None,
            pattern=DriftPattern.STABLE,
            thresholds={"warn": "-0.5%/week", "critical": "-1.0%/week"},
            projection_config={"exhaustion_warn": "30d", "exhaustion_critical": "14d"},
        )
        assert severity == DriftSeverity.NONE

    def test_generate_summary_none(self):
        analyzer = DriftAnalyzer(prometheus_url="http://localhost:9090")
        metrics = DriftMetrics(0, 0, 0.9, 0.90, 0.90, 0.001, 720)
        summary = analyzer._generate_summary(metrics, DriftPattern.STABLE, DriftSeverity.NONE)
        assert "stable" in summary.lower()

    def test_generate_summary_decline(self):
        analyzer = DriftAnalyzer(prometheus_url="http://localhost:9090")
        metrics = DriftMetrics(-0.001, -0.007, 0.85, 0.72, 0.80, 0.001, 720)
        summary = analyzer._generate_summary(
            metrics, DriftPattern.GRADUAL_DECLINE, DriftSeverity.WARN
        )
        assert "declining" in summary.lower()

    async def test_analyze_insufficient_data(self):
        analyzer = DriftAnalyzer(prometheus_url="http://localhost:9090")
        with patch.object(analyzer, "_query_budget_history") as mock_query:
            mock_query.return_value = [(datetime.now(), 0.80)]
            with pytest.raises(DriftAnalysisError, match="Insufficient data points"):
                await analyzer.analyze(service_name="test-service", tier="critical")


class TestDriftCLI:
    def test_drift_help(self, capsys):
        from nthlayer_workers.observe.cli import main

        with pytest.raises(SystemExit) as exc_info:
            main(["drift", "--help"])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "--service" in captured.out
        assert "--prometheus-url" in captured.out
        assert "--tier" in captured.out
