"""Tests for OverrideCalibration — MAE with known overrides."""

import pytest
import pytest_asyncio

from nthlayer_workers.measure.calibration.loop import OverrideCalibration
from nthlayer_workers.measure.store.sqlite import SQLiteScoreStore
from nthlayer_workers.measure.types import QualityScore


@pytest_asyncio.fixture
async def store(tmp_path):
    s = SQLiteScoreStore(tmp_path / "test.db")
    yield s
    s.close()


@pytest_asyncio.fixture
async def calibration(store):
    return OverrideCalibration(store)


def _make_score(eval_id: str, dims: dict[str, float]) -> QualityScore:
    return QualityScore(
        eval_id=eval_id,
        agent_name="agent-a",
        task_id="t1",
        dimensions=dims,
        confidence=0.8,
        evaluator_model="test",
    )


@pytest.mark.asyncio
async def test_no_overrides(calibration):
    report = await calibration.calibrate(window_days=30)
    assert report.total_overrides == 0
    assert report.mean_absolute_error == 0.0
    assert report.dimensions_analyzed == []


@pytest.mark.asyncio
async def test_single_override_mae(store, calibration):
    await store.save_score(_make_score("e1", {"correctness": 0.9}))
    await store.save_override("e1", {"correctness": 0.5}, "human")

    report = await calibration.calibrate(window_days=30)
    assert report.total_overrides == 1
    assert report.mean_absolute_error == pytest.approx(0.4)
    assert "correctness" in report.dimensions_analyzed


@pytest.mark.asyncio
async def test_multiple_overrides_mae(store, calibration):
    await store.save_score(_make_score("e1", {"correctness": 0.9, "style": 0.8}))
    await store.save_override("e1", {"correctness": 0.5, "style": 0.6}, "human")

    report = await calibration.calibrate(window_days=30)
    assert report.total_overrides == 2
    # MAE: (|0.9-0.5| + |0.8-0.6|) / 2 = (0.4 + 0.2) / 2 = 0.3
    assert report.mean_absolute_error == pytest.approx(0.3)
    assert sorted(report.dimensions_analyzed) == ["correctness", "style"]
