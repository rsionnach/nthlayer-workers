"""Tests for Judgment SLO checker — false accept rate, precision, recall, windowed compliance."""

import pytest
import pytest_asyncio

from nthlayer_workers.measure.calibration.slos import JudgmentSLOChecker
from nthlayer_workers.measure.manifest import JudgmentSLO
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


def _slo_with_quality_threshold(threshold: float = 0.5) -> JudgmentSLO:
    return JudgmentSLO(
        agent_name="agent-a",
        reversal_rate_target=0.05,
        reversal_rate_window_days=30,
        high_confidence_failure_target=0.02,
        confidence_threshold=0.9,
        quality_threshold=threshold,
    )


@pytest.mark.asyncio
async def test_false_accept_rate(store):
    """Evals overridden downward that had high avg scores should count as false accepts."""
    # Score e1: high quality (avg 0.85) — later overridden down
    await store.save_score(_score("e1", dims={"correctness": 0.9, "style": 0.8}))
    await store.save_override("e1", {"correctness": 0.3}, "human")

    # Score e2: high quality (avg 0.85) — no override
    await store.save_score(_score("e2", dims={"correctness": 0.9, "style": 0.8}))

    checker = JudgmentSLOChecker(store, slo=_slo_with_quality_threshold(0.5))
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

    checker = JudgmentSLOChecker(store, slo=_slo_with_quality_threshold(0.5))
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

    checker = JudgmentSLOChecker(store, slo=_slo_with_quality_threshold(0.5))
    report = await checker.check("agent-a", window_days=7)

    # 2 human-flagged (downward overrides), 1 evaluator caught (low score) → recall = 0.5
    assert report.recall == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_windowed_compliance(store):
    """Windowed compliance checks reversal rate against SLO target."""
    # 2 evals, 1 with override → 50% reversal rate
    await store.save_score(_score("e1"))
    await store.save_score(_score("e2"))
    await store.save_override("e1", {"correctness": 0.3}, "human")

    slo = JudgmentSLO(
        agent_name="agent-a",
        reversal_rate_target=0.6,
        reversal_rate_window_days=30,
        high_confidence_failure_target=0.02,
        confidence_threshold=0.9,
    )

    checker = JudgmentSLOChecker(store, slo=slo)
    report = await checker.check("agent-a", window_days=7)

    assert report.reversal_rate == pytest.approx(0.5)
    assert report.reversal_rate_target == pytest.approx(0.6)
    assert report.reversal_rate_compliant is True  # 0.5 <= 0.6


@pytest.mark.asyncio
async def test_slo_report_no_manifest_fails_open(store):
    """Without a manifest quality_threshold, threshold-dependent metrics are None."""
    await store.save_score(_score("e1"))
    await store.save_score(_score("e2"))

    checker = JudgmentSLOChecker(store)
    report = await checker.check("agent-a", window_days=7)

    assert report.reversal_rate == 0.0
    assert report.false_accept_rate is None
    assert report.precision is None
    assert report.recall is None
    assert report.mae == 0.0
    assert report.total_evaluations == 2
    assert report.total_overrides == 0


@pytest.mark.asyncio
async def test_slo_report_with_quality_threshold(store):
    """With a manifest quality_threshold, threshold-dependent metrics are computed."""
    await store.save_score(_score("e1"))
    await store.save_score(_score("e2"))

    checker = JudgmentSLOChecker(store, slo=_slo_with_quality_threshold(0.5))
    report = await checker.check("agent-a", window_days=7)

    assert report.reversal_rate == 0.0
    assert report.false_accept_rate == 0.0
    assert report.precision == 1.0
    assert report.recall == 1.0
    assert report.mae == 0.0


@pytest.mark.asyncio
async def test_slo_report_with_manifest_target(store):
    """Report with manifest target should include compliance."""
    await store.save_score(_score("e1"))

    slo = JudgmentSLO(
        agent_name="agent-a",
        reversal_rate_target=0.05,
        reversal_rate_window_days=30,
        high_confidence_failure_target=0.02,
        confidence_threshold=0.9,
    )

    checker = JudgmentSLOChecker(store, slo=slo)
    report = await checker.check("agent-a", window_days=7)

    assert report.reversal_rate_target == pytest.approx(0.05)
    assert report.reversal_rate_compliant is True  # 0% reversal <= 5% target


@pytest.mark.asyncio
async def test_slo_report_without_manifest(store):
    """Report without manifest should have None for target/compliance."""
    await store.save_score(_score("e1"))

    checker = JudgmentSLOChecker(store)
    report = await checker.check("agent-a", window_days=7)

    assert report.reversal_rate_target is None
    assert report.reversal_rate_compliant is None
