"""Tests for ThresholdDetector — degradation detection against SLO thresholds."""


from nthlayer_workers.measure.detection.detector import SLOThresholds, ThresholdDetector
from nthlayer_workers.measure.types import TrendWindow


def _make_window(**kwargs) -> TrendWindow:
    defaults = dict(
        agent_name="agent-a",
        window_days=7,
        dimension_averages={"correctness": 0.9, "style": 0.8},
        evaluation_count=10,
        confidence_mean=0.85,
        reversal_rate=0.1,
        total_cost_usd=0.5,
        avg_cost_per_eval=0.05,
    )
    defaults.update(kwargs)
    return TrendWindow(**defaults)


def test_no_alerts_within_slo():
    thresholds = SLOThresholds(
        max_reversal_rate=0.3,
        min_dimension_scores={"correctness": 0.7, "style": 0.6},
        min_confidence=0.5,
    )
    detector = ThresholdDetector(thresholds)
    alerts = detector.check(_make_window())
    assert alerts == []


def test_reversal_rate_alert():
    thresholds = SLOThresholds(max_reversal_rate=0.05)
    detector = ThresholdDetector(thresholds)
    alerts = detector.check(_make_window(reversal_rate=0.15))
    assert len(alerts) == 1
    assert alerts[0].metric_name == "reversal_rate"
    assert alerts[0].current_value == 0.15
    assert alerts[0].threshold == 0.05


def test_dimension_below_slo():
    thresholds = SLOThresholds(min_dimension_scores={"correctness": 0.95})
    detector = ThresholdDetector(thresholds)
    alerts = detector.check(_make_window(dimension_averages={"correctness": 0.8}))
    assert len(alerts) == 1
    assert alerts[0].metric_name == "dimension:correctness"
    assert alerts[0].current_value == 0.8


def test_empty_window_no_alerts():
    thresholds = SLOThresholds(max_reversal_rate=0.01, min_confidence=0.99)
    detector = ThresholdDetector(thresholds)
    alerts = detector.check(_make_window(evaluation_count=0))
    assert alerts == []


def test_confidence_below_slo():
    thresholds = SLOThresholds(min_confidence=0.9)
    detector = ThresholdDetector(thresholds)
    alerts = detector.check(_make_window(confidence_mean=0.5))
    assert len(alerts) == 1
    assert alerts[0].metric_name == "confidence"
