"""Calibration metrics per agent: evaluation override rate, false accept rate,
precision, recall.

None of these is A1's reversal rate (reversals / all decisions), and the
report judges no SLO: the manifest's reversal-rate SLO is evaluated by the
measure module from the A1 series [opensrm-l0ki].

All metrics are arithmetic over stored scores and overrides (ZFC: transport).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from nthlayer_workers.measure.store.protocol import ScoreStore
from nthlayer_workers.measure.telemetry import emit_calibration_report_event
from nthlayer_workers.measure.types import QualityScore


@dataclass(frozen=True)
class JudgmentSLOReport:
    """Calibration report for one agent."""

    agent_name: str
    window_days: int
    # Overridden evaluations / all evaluations in the window.
    evaluation_override_rate: float
    false_accept_rate: float | None
    precision: float | None
    recall: float | None
    mae: float
    total_evaluations: int
    total_overrides: int


class JudgmentSLOChecker:
    """Computes the calibration report from store data.

    *quality_threshold* is the score below which an evaluation counts as
    rejected. Nothing declares one today (no manifest format has it, and the
    CLI passes none), so without it the false-accept rate, precision and
    recall are None.
    """

    def __init__(self, store: ScoreStore, quality_threshold: float | None = None) -> None:
        self._store = store
        self._quality_threshold = quality_threshold

    async def check(
        self,
        agent_name: str,
        window_days: int = 30,
    ) -> JudgmentSLOReport:
        since = datetime.now(UTC) - timedelta(days=window_days)

        scores = await self._store.get_scores(agent_name, since=since, limit=100000)
        overrides = await self._store.get_overrides(
            since=since, limit=100000, agent_name=agent_name
        )

        total_evals = len(scores)
        total_overrides_count = len(overrides)

        # Build lookup: eval_id -> QualityScore.dimensions
        score_by_id: dict[str, dict[str, float]] = {}
        for s in scores:
            score_by_id[s.eval_id] = s.dimensions

        # Build override groups: eval_id -> list of override dicts
        overrides_by_eval: dict[str, list[dict]] = {}
        for ov in overrides:
            eid = ov["eval_id"]
            overrides_by_eval.setdefault(eid, []).append(ov)

        evaluation_override_rate = self._compute_evaluation_override_rate(
            overrides_by_eval, total_evals
        )
        mae = self._compute_mae(overrides)

        # Fail open (None) when absent: without an operator-declared
        # threshold, code cannot classify quality.
        score_threshold = self._quality_threshold

        if score_threshold is not None:
            false_accept_rate = self._compute_false_accept_rate(
                overrides_by_eval, score_by_id, score_threshold
            )
            precision = self._compute_precision(
                scores, overrides_by_eval, score_threshold
            )
            recall = self._compute_recall(
                overrides_by_eval, score_by_id, score_threshold
            )
        else:
            false_accept_rate = None
            precision = None
            recall = None

        report = JudgmentSLOReport(
            agent_name=agent_name,
            window_days=window_days,
            evaluation_override_rate=evaluation_override_rate,
            false_accept_rate=false_accept_rate,
            precision=precision,
            recall=recall,
            mae=mae,
            total_evaluations=total_evals,
            total_overrides=total_overrides_count,
        )

        emit_calibration_report_event(
            agent_name=agent_name,
            window_days=window_days,
            evaluation_override_rate=evaluation_override_rate,
            false_accept_rate=false_accept_rate,
            precision=precision,
            recall=recall,
            mae=mae,
        )

        return report

    @staticmethod
    def _compute_evaluation_override_rate(
        overrides_by_eval: dict[str, list[dict]], total_evals: int
    ) -> float:
        if total_evals == 0:
            return 0.0
        return len(overrides_by_eval) / total_evals

    @staticmethod
    def _compute_false_accept_rate(
        overrides_by_eval: dict[str, list[dict]],
        score_by_id: dict[str, dict[str, float]],
        score_threshold: float,
    ) -> float:
        downward_eval_ids: set[str] = set()
        for eid, ovs in overrides_by_eval.items():
            for ov in ovs:
                if ov["corrected_score"] < ov["original_score"]:
                    downward_eval_ids.add(eid)
                    break

        if not downward_eval_ids:
            return 0.0

        false_accepts = 0
        for eid in downward_eval_ids:
            dims = score_by_id.get(eid, {})
            if dims:
                avg = sum(dims.values()) / len(dims)
                if avg >= score_threshold:
                    false_accepts += 1

        return false_accepts / len(downward_eval_ids)

    @staticmethod
    def _compute_precision(
        scores: list[QualityScore],
        overrides_by_eval: dict[str, list[dict]],
        score_threshold: float,
    ) -> float:
        scored_low_ids: list[str] = []
        for s in scores:
            if s.dimensions:
                avg = sum(s.dimensions.values()) / len(s.dimensions)
                if avg < score_threshold:
                    scored_low_ids.append(s.eval_id)

        if not scored_low_ids:
            return 1.0

        agreed_low = 0
        for eid in scored_low_ids:
            has_upward = False
            for ov in overrides_by_eval.get(eid, []):
                if ov["corrected_score"] > ov["original_score"]:
                    has_upward = True
                    break
            if not has_upward:
                agreed_low += 1
        return agreed_low / len(scored_low_ids)

    @staticmethod
    def _compute_recall(
        overrides_by_eval: dict[str, list[dict]],
        score_by_id: dict[str, dict[str, float]],
        score_threshold: float,
    ) -> float:
        downward_eval_ids: set[str] = set()
        for eid, ovs in overrides_by_eval.items():
            for ov in ovs:
                if ov["corrected_score"] < ov["original_score"]:
                    downward_eval_ids.add(eid)
                    break

        if not downward_eval_ids:
            return 1.0

        caught = 0
        for eid in downward_eval_ids:
            dims = score_by_id.get(eid, {})
            if dims:
                avg = sum(dims.values()) / len(dims)
                if avg < score_threshold:
                    caught += 1
        return caught / len(downward_eval_ids)

    @staticmethod
    def _compute_mae(overrides: list[dict]) -> float:
        if not overrides:
            return 0.0
        return sum(
            abs(ov["original_score"] - ov["corrected_score"]) for ov in overrides
        ) / len(overrides)
