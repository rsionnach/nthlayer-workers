"""Self-calibration loop — measures nthlayer-measure's own judgment accuracy.

Uses override data (human corrections) to compute how well the evaluator's
scores match ground truth. Pure arithmetic over stored data (ZFC).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from nthlayer_workers.measure.store.protocol import ScoreStore


@dataclass(frozen=True)
class CalibrationReport:
    """Results of a calibration run."""

    total_overrides: int
    mean_absolute_error: float
    dimensions_analyzed: list[str]


class CalibrationLoop(Protocol):
    """Computes evaluator accuracy from override history."""

    async def calibrate(self, window_days: int = 30) -> CalibrationReport: ...


class OverrideCalibration:
    """Calibration based on comparing original scores to human overrides."""

    def __init__(self, store: ScoreStore) -> None:
        self._store = store

    async def calibrate(self, window_days: int = 30) -> CalibrationReport:
        since = datetime.now(UTC) - timedelta(days=window_days)
        overrides = await self._store.get_overrides(since=since, limit=10000)

        if not overrides:
            return CalibrationReport(
                total_overrides=0,
                mean_absolute_error=0.0,
                dimensions_analyzed=[],
            )

        errors: list[float] = []
        dimensions_seen: set[str] = set()

        for ov in overrides:
            error = abs(ov["original_score"] - ov["corrected_score"])
            errors.append(error)
            dimensions_seen.add(ov["dimension"])

        mae = sum(errors) / len(errors)

        return CalibrationReport(
            total_overrides=len(overrides),
            mean_absolute_error=mae,
            dimensions_analyzed=sorted(dimensions_seen),
        )
