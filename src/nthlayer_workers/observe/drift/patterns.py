"""Pattern detection for drift analysis.

Classifies drift patterns beyond simple linear trends:
- Gradual decline/improvement
- Step changes (sudden drops or improvements)
- Volatile patterns
- Stable (no significant trend)
"""

from __future__ import annotations

from datetime import datetime

import numpy as np

from nthlayer_workers.observe.drift.models import DriftPattern


class PatternDetector:
    """Detect drift patterns beyond simple linear trends."""

    def __init__(
        self,
        step_change_threshold: float = 0.05,
        volatility_variance_threshold: float = 0.01,
        volatility_r_squared_threshold: float = 0.3,
        slope_significance_threshold: float = 0.001,
    ):
        self.step_change_threshold = step_change_threshold
        self.volatility_variance_threshold = volatility_variance_threshold
        self.volatility_r_squared_threshold = volatility_r_squared_threshold
        self.slope_significance_threshold = slope_significance_threshold

    def detect(
        self,
        data: list[tuple[datetime, float]],
        slope_per_second: float,
        r_squared: float,
    ) -> DriftPattern:
        """Classify the drift pattern."""
        if len(data) < 2:
            return DriftPattern.STABLE

        values = np.array([d[1] for d in data])
        variance = float(np.var(values))

        step_change = self._detect_step_change(data)
        if step_change is not None:
            return step_change

        if (
            r_squared < self.volatility_r_squared_threshold
            and variance > self.volatility_variance_threshold
        ):
            return DriftPattern.VOLATILE

        seconds_per_week = 7 * 24 * 60 * 60
        weekly_slope = slope_per_second * seconds_per_week

        if abs(weekly_slope) < self.slope_significance_threshold:
            return DriftPattern.STABLE
        elif weekly_slope < 0:
            return DriftPattern.GRADUAL_DECLINE
        else:
            return DriftPattern.GRADUAL_IMPROVEMENT

    def _detect_step_change(
        self,
        data: list[tuple[datetime, float]],
    ) -> DriftPattern | None:
        """Detect sudden step changes in the data."""
        if len(data) < 2:
            return None

        max_time_window = 86400 * 1.5

        for i in range(1, len(data)):
            time_diff = (data[i][0] - data[i - 1][0]).total_seconds()
            value_diff = data[i][1] - data[i - 1][1]

            if time_diff < max_time_window:
                if value_diff < -self.step_change_threshold:
                    return DriftPattern.STEP_CHANGE_DOWN
                elif value_diff > self.step_change_threshold:
                    return DriftPattern.STEP_CHANGE_UP

        return None

