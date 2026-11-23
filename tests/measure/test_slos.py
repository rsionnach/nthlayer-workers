"""Tests for the calibrate report: evaluation override rate, false accept rate,
precision, recall."""

import dataclasses

import pytest
import pytest_asyncio

from nthlayer_workers.measure.calibration.slos import JudgmentSLOChecker, JudgmentSLOReport
from nthlayer_workers.measure.store.sqlite import SQLiteScoreStore
from nthlayer_workers.measure.types import QualityScore


@pytest_asyncio.fixture
async def store(tmp_path):
    s = SQLiteScoreStore(tmp_path / "test.db")
    yield s
    s.close()


def _score(eval_id: str, agent: str = "agent-a", dims: dict | None = None, confidence: float = 0.85) -> QualityScore:
    return QualityScore(
        eval_id=eval_id,
        agent_name=agent,
        task_id="t1",
        dimensions=dims or {"correctness": 0.9, "style": 0.8},
        confidence=confidence,
        evaluator_model="test-model",
        cost_usd=0.01,
    )


@pytest.mark.asyncio
async def test_false_accept_rate(store):
    """Evals overridden downward that had high avg scores should count as false accepts."""
    # Score e1: high quality (avg 0.85) — later overridden down
    await store.save_score(_score("e1", dims={"correctness": 0.9, "style": 0.8}))
    await store.save_override("e1", {"correctness": 0.3}, "human")

    # Score e2: high quality (avg 0.85) — no override
    await store.save_score(_score("e2", dims={"correctness": 0.9, "style": 0.8}))

    checker = JudgmentSLOChecker(store, quality_threshold=(0.5))
    report = await checker.check("agent-a", window_days=7)

    # 1 downward override, original avg 0.85 >= 0.5 → false accept
    assert report.false_accept_rate == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_precision(store):
    """Precision: of evals scored low, how many did human agree are low?"""
    # e1: low score, no upward override → human agrees
    await store.save_score(_score("e1", dims={"correctness": 0.3, "style": 0.2}))

    # e2: low score, upward override → human disagrees
    await store.save_score(_score("e2", dims={"correctness": 0.3, "style": 0.2}))
    await store.save_override("e2", {"correctness": 0.9}, "human")

    checker = JudgmentSLOChecker(store, quality_threshold=(0.5))
    report = await checker.check("agent-a", window_days=7)

    # 2 scored low, 1 agreed → precision = 0.5
    assert report.precision == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_recall(store):
    """Recall: of human-flagged evals, what fraction did evaluator catch?"""
    # e1: high score, overridden down → human flagged, evaluator missed
    await store.save_score(_score("e1", dims={"correctness": 0.9, "style": 0.8}))
    await store.save_override("e1", {"correctness": 0.2}, "human")

    # e2: low score, overridden down → human flagged, evaluator caught
    await store.save_score(_score("e2", dims={"correctness": 0.3, "style": 0.2}))
    await store.save_override("e2", {"correctness": 0.1}, "human")

    checker = JudgmentSLOChecker(store, quality_threshold=(0.5))
    report = await checker.check("agent-a", window_days=7)

    # 2 human-flagged (downward overrides), 1 evaluator caught (low score) → recall = 0.5
    assert report.recall == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_evaluation_override_rate_is_overridden_evals_over_all_evals(store):
    # 4 evals; e1 overridden twice and e2 once: 2 distinct overridden evals.
    for eid in ("e1", "e2", "e3", "e4"):
        await store.save_score(_score(eid))
    await store.save_override("e1", {"correctness": 0.3}, "human")
    await store.save_override("e1", {"style": 0.2}, "human")
    await store.save_override("e2", {"correctness": 0.4}, "human")

    report = await JudgmentSLOChecker(store).check("agent-a", window_days=7)

    assert report.evaluation_override_rate == pytest.approx(0.5)
    assert report.total_evaluations == 4
    assert report.total_overrides == 3


@pytest.mark.asyncio
async def test_evaluation_override_rate_with_no_evaluations_is_zero(store):
    report = await JudgmentSLOChecker(store).check("agent-a", window_days=7)

    assert report.evaluation_override_rate == 0.0
    assert report.total_evaluations == 0


def test_the_report_names_no_reversal_rate_and_judges_no_slo():
    """A1's reversals over all decisions is the only quantity named reversal
    rate, and calibrate does not judge the manifest's SLO for it
    [opensrm-l0ki, from the opensrm-5lxs decision record]."""
    assert {f.name for f in dataclasses.fields(JudgmentSLOReport)} == {
        "agent_name", "window_days", "evaluation_override_rate",
        "false_accept_rate", "precision", "recall", "mae",
        "total_evaluations", "total_overrides",
    }


@pytest.mark.asyncio
async def test_report_without_a_quality_threshold_fails_open(store):
    """Without a quality threshold, threshold-dependent metrics are None."""
    await store.save_score(_score("e1"))
    await store.save_score(_score("e2"))

    checker = JudgmentSLOChecker(store)
    report = await checker.check("agent-a", window_days=7)

    assert report.evaluation_override_rate == 0.0
    assert report.false_accept_rate is None
    assert report.precision is None
    assert report.recall is None
    assert report.mae == 0.0
    assert report.total_evaluations == 2
    assert report.total_overrides == 0


@pytest.mark.asyncio
async def test_report_with_quality_threshold(store):
    """With a quality threshold, threshold-dependent metrics are computed."""
    await store.save_score(_score("e1"))
    await store.save_score(_score("e2"))

    checker = JudgmentSLOChecker(store, quality_threshold=(0.5))
    report = await checker.check("agent-a", window_days=7)

    assert report.evaluation_override_rate == 0.0
    assert report.false_accept_rate == 0.0
    assert report.precision == 1.0
    assert report.recall == 1.0
    assert report.mae == 0.0
