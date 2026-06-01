"""Core drift analysis logic.

Analyzes SLO drift over time using Prometheus range queries,
calculating trends, projections, and severity classifications.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
from scipy import stats

from nthlayer_workers.observe.drift.models import (
    DriftMetrics,
    DriftPattern,
    DriftProjection,
    DriftResult,
    DriftSeverity,
    get_drift_defaults,
)
from nthlayer_workers.observe.drift.patterns import PatternDetector


class DriftAnalysisError(Exception):
    """Error during drift analysis."""

    pass


class DriftAnalyzer:
    """Analyzes SLO drift over time using Prometheus range queries."""

    def __init__(
        self,
        prometheus_url: str,
        username: str | None = None,
        password: str | None = None,
    ):
        self.prometheus_url = prometheus_url.rstrip("/")
        self.username = username
        self.password = password

    def _get_pattern_detector(self, config: dict[str, Any]) -> PatternDetector:
        """Get pattern detector with configuration."""
        patterns_config = config.get("patterns", {})
        return PatternDetector(
            step_change_threshold=patterns_config.get("step_change_threshold", 0.05),
        )

    async def analyze(
        self,
        service_name: str,
        tier: str,
        slo: str = "availability",
        window: str | None = None,
        thresholds: dict[str, str] | None = None,
        projection_config: dict[str, str] | None = None,
        drift_config: dict[str, Any] | None = None,
    ) -> DriftResult:
        """Analyze drift for a service's SLO."""
        defaults = get_drift_defaults(tier)

        config = {**defaults, **drift_config} if drift_config else defaults.copy()

        analysis_window = window or config["window"]
        analysis_thresholds = thresholds or config["thresholds"]
        analysis_projection = projection_config or config["projection"]

        try:
            data = await self._query_budget_history(service_name, analysis_window, slo)
        except Exception as e:
            raise DriftAnalysisError(f"Failed to query Prometheus: {e}") from e

        if len(data) < 2:
            raise DriftAnalysisError(
                f"Insufficient data points for {service_name}/{slo}. "
                f"Need at least 2 data points, got {len(data)}"
            )

        slope_per_second, intercept, r_squared = self._calculate_trend(data)

        slope_per_day = slope_per_second * 86400
        slope_per_week = slope_per_second * 86400 * 7

        current_budget = data[-1][1]
        budget_at_start = data[0][1]

        values = np.array([d[1] for d in data])
        variance = float(np.var(values))

        metrics = DriftMetrics(
            slope_per_day=slope_per_day,
            slope_per_week=slope_per_week,
            r_squared=r_squared,
            current_budget=current_budget,
            budget_at_window_start=budget_at_start,
            variance=variance,
            data_points=len(data),
        )

        days_until_exhaustion = self._project_exhaustion(current_budget, slope_per_second)
        projection = DriftProjection(
            days_until_exhaustion=days_until_exhaustion,
            projected_budget_30d=max(0, current_budget + slope_per_day * 30),
            projected_budget_60d=max(0, current_budget + slope_per_day * 60),
            projected_budget_90d=max(0, current_budget + slope_per_day * 90),
            confidence=r_squared,
        )

        detector = self._get_pattern_detector(config)
        pattern = detector.detect(data, slope_per_second, r_squared)

        severity = self._classify_severity(
            slope_per_week=slope_per_week,
            days_until_exhaustion=days_until_exhaustion,
            pattern=pattern,
            thresholds=analysis_thresholds,
            projection_config=analysis_projection,
        )

        summary = self._generate_summary(metrics, pattern, severity)
        recommendation = self._generate_recommendation(pattern, severity, metrics)

        exit_code = {
            DriftSeverity.NONE: 0,
            DriftSeverity.INFO: 0,
            DriftSeverity.WARN: 1,
            DriftSeverity.CRITICAL: 2,
        }[severity]

        return DriftResult(
            service_name=service_name,
            tier=tier,
            slo_name=slo,
            window=analysis_window,
            analyzed_at=datetime.now(UTC),
            data_start=data[0][0],
            data_end=data[-1][0],
            metrics=metrics,
            projection=projection,
            pattern=pattern,
            severity=severity,
            summary=summary,
            recommendation=recommendation,
            exit_code=exit_code,
        )

    async def _query_budget_history(
        self,
        service: str,
        window: str,
        slo: str = "availability",
        step: str = "1h",
    ) -> list[tuple[datetime, float]]:
        """Query error budget over time window."""
        import httpx

        query = f'slo:error_budget_remaining:ratio{{service="{service}", slo="{slo}"}}'

        end = datetime.now(UTC)
        start = end - self._parse_duration(window)

        params: dict[str, str] = {
            "query": query,
            "start": str(start.timestamp()),
            "end": str(end.timestamp()),
            "step": step,
        }

        auth = None
        if self.username and self.password:
            auth = (self.username, self.password)

        async with httpx.AsyncClient(auth=auth, timeout=30.0) as client:
            response = await client.get(
                f"{self.prometheus_url}/api/v1/query_range",
                params=params,
            )
            response.raise_for_status()
            result = response.json()

        if result.get("status") != "success":
            raise DriftAnalysisError(f"Prometheus query failed: {result.get('error', 'Unknown')}")

        data = result.get("data", {})
        results = data.get("result", [])

        if not results:
            raise DriftAnalysisError(f"No data returned for {service}/{slo}")

        values = results[0].get("values", [])
        parsed = []
        for ts, val in values:
            v = float(val)
            if math.isfinite(v):
                parsed.append((datetime.fromtimestamp(float(ts), tz=UTC), v))
        return parsed

    def _calculate_trend(
        self,
        data: list[tuple[datetime, float]],
    ) -> tuple[float, float, float]:
        """Calculate linear trend using least squares regression."""
        if len(data) < 2:
            raise DriftAnalysisError("Insufficient data points for trend analysis")

        timestamps = np.array([d[0].timestamp() for d in data])
        values = np.array([d[1] for d in data])

        timestamps = timestamps - timestamps[0]

        result = stats.linregress(timestamps, values)
        slope = result.slope
        intercept = result.intercept
        r_squared = result.rvalue**2

        return slope, intercept, r_squared

    def _project_exhaustion(
        self,
        current_budget: float,
        slope_per_second: float,
    ) -> int | None:
        """Project days until budget exhaustion."""
        if slope_per_second >= 0:
            return None

        if current_budget <= 0:
            return 0

        seconds_to_exhaustion = current_budget / abs(slope_per_second)
        days_to_exhaustion = seconds_to_exhaustion / 86400

        if days_to_exhaustion > 365:
            return None

        return int(days_to_exhaustion)

    def _classify_severity(
        self,
        slope_per_week: float,
        days_until_exhaustion: int | None,
        pattern: DriftPattern,
        thresholds: dict[str, str],
        projection_config: dict[str, str],
    ) -> DriftSeverity:
        """Classify drift severity."""
        warn_slope = self._parse_threshold(thresholds["warn"])
        critical_slope = self._parse_threshold(thresholds["critical"])
        exhaustion_warn = self._parse_days(projection_config["exhaustion_warn"])
        exhaustion_critical = self._parse_days(projection_config["exhaustion_critical"])

        if days_until_exhaustion is not None and days_until_exhaustion <= exhaustion_critical:
            return DriftSeverity.CRITICAL

        if pattern == DriftPattern.STEP_CHANGE_DOWN:
            return DriftSeverity.CRITICAL

        if slope_per_week <= critical_slope:
            return DriftSeverity.CRITICAL

        if days_until_exhaustion is not None and days_until_exhaustion <= exhaustion_warn:
            return DriftSeverity.WARN

        if slope_per_week <= warn_slope:
            return DriftSeverity.WARN

        if slope_per_week < 0:
            return DriftSeverity.INFO

        return DriftSeverity.NONE

    def _parse_threshold(self, threshold: str) -> float:
        """Parse threshold string like '-0.5%/week' to float."""
        value = threshold.replace("/week", "").replace("%", "").strip()
        return float(value) / 100

    def _parse_days(self, duration: str) -> int:
        """Parse duration string like '30d' to integer days."""
        match = re.match(r"(\d+)d", duration)
        if match:
            return int(match.group(1))
        return 30

    def _parse_duration(self, duration: str) -> timedelta:
        """Parse duration string to timedelta.

        Note: 'm' means months (30 days) here, not minutes.
        Drift analysis windows are days/weeks/months, not minutes.
        """
        match = re.match(r"(\d+)([dhwm])", duration)
        if not match:
            return timedelta(days=30)

        value = int(match.group(1))
        unit = match.group(2)

        units = {"d": timedelta(days=value), "h": timedelta(hours=value), "w": timedelta(weeks=value), "m": timedelta(days=value * 30)}
        return units.get(unit, timedelta(days=30))

    def _generate_summary(
        self,
        metrics: DriftMetrics,
        pattern: DriftPattern,
        severity: DriftSeverity,
    ) -> str:
        """Generate human-readable summary."""
        slope_pct = abs(metrics.slope_per_week * 100)
        direction = "declining" if metrics.slope_per_week < 0 else "improving"

        if severity == DriftSeverity.NONE:
            return "Error budget is stable with no significant drift detected."

        if severity == DriftSeverity.INFO:
            return (
                f"Minor budget drift detected: {direction} at {slope_pct:.2f}% per week. "
                f"Fit quality: R²={metrics.r_squared:.2f}"
            )

        if pattern == DriftPattern.STEP_CHANGE_DOWN:
            return (
                f"Sudden budget drop detected! "
                f"Budget changed from {metrics.budget_at_window_start:.1%} "
                f"to {metrics.current_budget:.1%}."
            )

        confidence = "high" if metrics.r_squared > 0.7 else "moderate"
        return (
            f"Error budget {direction} at {slope_pct:.2f}% per week "
            f"with {confidence} confidence (R²={metrics.r_squared:.2f})."
        )

    def _generate_recommendation(
        self,
        pattern: DriftPattern,
        severity: DriftSeverity,
        metrics: DriftMetrics,
    ) -> str:
        """Generate actionable recommendation."""
        if severity == DriftSeverity.NONE:
            return "No action needed. Continue monitoring."

        if severity == DriftSeverity.INFO:
            return (
                "Monitor for continued decline. Consider reviewing recent deployments "
                "if trend persists."
            )

        if pattern == DriftPattern.STEP_CHANGE_DOWN:
            return (
                "Investigate immediate cause of step change. Check recent deployments, "
                "configuration changes, or dependency issues. Run `nthlayer-observe verify` "
                "to check metric coverage."
            )

        if pattern == DriftPattern.VOLATILE:
            return (
                "High variance suggests intermittent issues. Review error logs and "
                "identify patterns in failures. Consider adjusting SLO alerting windows."
            )

        recommendations = [
            "Investigate recent changes.",
            "Common causes: increased traffic, dependency degradation, or configuration drift.",
        ]

        if metrics.r_squared > 0.7:
            recommendations.append(
                "High confidence in trend - proactive investigation recommended."
            )

        recommendations.append("Run `nthlayer-observe verify` to check metric coverage.")

        return " ".join(recommendations)
