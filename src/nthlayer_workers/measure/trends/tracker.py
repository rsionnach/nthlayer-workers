"""Trend tracking — pure arithmetic over stored scores (ZFC: transport, not judgment)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol

from nthlayer_workers.measure.store.protocol import ScoreStore
from nthlayer_workers.measure.types import TrendWindow


class TrendTracker(Protocol):
    """Computes aggregate trends over evaluation windows.

    Pure arithmetic — averages, rates, counts. Never interprets
    whether a trend is "good" or "bad" (that's the model's job).
    """

    async def compute_window(self, agent_name: str, window_days: int) -> TrendWindow: ...


class StoreTrendTracker:
    """TrendTracker backed by a ScoreStore."""

    def __init__(self, store: ScoreStore) -> None:
        self._store = store

    async def compute_window(self, agent_name: str, window_days: int) -> TrendWindow:
        since = datetime.now(UTC) - timedelta(days=window_days)
        scores = await self._store.get_scores(agent_name, since=since, limit=10000)

        if not scores:
            return TrendWindow(
                agent_name=agent_name,
                window_days=window_days,
                dimension_averages={},
                evaluation_count=0,
                confidence_mean=0.0,
            )

        dim_totals: dict[str, float] = {}
        dim_counts: dict[str, int] = {}
        confidence_sum = 0.0
        total_cost = 0.0

        for score in scores:
            confidence_sum += score.confidence
            total_cost += score.cost_usd or 0.0
            for dim_name, dim_score in score.dimensions.items():
                dim_totals[dim_name] = dim_totals.get(dim_name, 0.0) + dim_score
                dim_counts[dim_name] = dim_counts.get(dim_name, 0) + 1

        dimension_averages = {
            name: dim_totals[name] / dim_counts[name] for name in dim_totals
        }

        # Reversal rate: proportion of eval_ids that have overrides
        overrides = await self._store.get_overrides(since, limit=10000, agent_name=agent_name)
        overridden_eval_ids = {o["eval_id"] for o in overrides}
        reversal_rate = len(overridden_eval_ids) / len(scores)

        count = len(scores)
        return TrendWindow(
            agent_name=agent_name,
            window_days=window_days,
            dimension_averages=dimension_averages,
            evaluation_count=count,
            confidence_mean=confidence_sum / count,
            reversal_rate=reversal_rate,
            total_cost_usd=total_cost,
            avg_cost_per_eval=total_cost / count,
        )
