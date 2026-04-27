"""Threshold-based degradation detector."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from nthlayer_workers.measure.detection.protocol import Alert
from nthlayer_workers.measure.types import TrendWindow

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SLOThresholds:
    """Human-declared service level objectives."""

    max_reversal_rate: float = 1.0
    min_dimension_scores: dict[str, float] = field(default_factory=dict)
    min_confidence: float = 0.0


class ThresholdDetector:
    """Compares TrendWindow arithmetic against SLOThresholds.

    All thresholds are human-declared. This code only does comparison (ZFC).
    """

    def __init__(self, thresholds: SLOThresholds) -> None:
        self._thresholds = thresholds

    def check(self, window: TrendWindow) -> list[Alert]:
        if window.evaluation_count == 0:
            return []

        alerts: list[Alert] = []

        if window.reversal_rate > self._thresholds.max_reversal_rate:
            alert = Alert(
                agent_name=window.agent_name,
                metric_name="reversal_rate",
                current_value=window.reversal_rate,
                threshold=self._thresholds.max_reversal_rate,
                message=(
                    f"Reversal rate {window.reversal_rate:.2%} exceeds "
                    f"threshold {self._thresholds.max_reversal_rate:.2%}"
                ),
            )
            alerts.append(alert)
            logger.warning("Degradation: %s", alert.message)

        if window.confidence_mean < self._thresholds.min_confidence:
            alert = Alert(
                agent_name=window.agent_name,
                metric_name="confidence",
                current_value=window.confidence_mean,
                threshold=self._thresholds.min_confidence,
                message=(
                    f"Confidence {window.confidence_mean:.2f} below "
                    f"threshold {self._thresholds.min_confidence:.2f}"
                ),
            )
            alerts.append(alert)
            logger.warning("Degradation: %s", alert.message)

        for dim_name, min_score in self._thresholds.min_dimension_scores.items():
            avg = window.dimension_averages.get(dim_name)
            if avg is not None and avg < min_score:
                alert = Alert(
                    agent_name=window.agent_name,
                    metric_name=f"dimension:{dim_name}",
                    current_value=avg,
                    threshold=min_score,
                    message=(
                        f"Dimension '{dim_name}' average {avg:.2f} below "
                        f"threshold {min_score:.2f}"
                    ),
                )
                alerts.append(alert)
                logger.warning("Degradation: %s", alert.message)

        return alerts
