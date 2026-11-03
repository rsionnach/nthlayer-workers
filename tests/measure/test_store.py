"""Tests for SQLiteScoreStore — round-trip save/get for scores, overrides, autonomy."""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from nthlayer_workers.measure.store.sqlite import SQLiteScoreStore
from nthlayer_workers.measure.types import QualityScore


@pytest_asyncio.fixture
async def store(tmp_path):
    s = SQLiteScoreStore(tmp_path / "test.db")
    yield s
    s.close()


def _make_score(eval_id: str = "e1", agent: str = "agent-a", task: str = "t1", **kwargs) -> QualityScore:
    return QualityScore(
        eval_id=eval_id,
        agent_name=agent,
        task_id=task,
        dimensions=kwargs.get("dimensions", {"correctness": 0.9, "style": 0.7}),
        reasoning=kwargs.get("reasoning", {"correctness": "Good", "style": "OK"}),
        confidence=kwargs.get("confidence", 0.85),
        evaluator_model=kwargs.get("evaluator_model", "test-model"),
        cost_usd=kwargs.get("cost_usd", 0.01),
    )


@pytest.mark.asyncio
async def test_save_and_get_score(store):
    score = _make_score()
    await store.save_score(score)

    since = datetime.now(UTC) - timedelta(hours=1)
    results = await store.get_scores("agent-a", since)

    assert len(results) == 1
    r = results[0]
    assert r.eval_id == "e1"
    assert r.agent_name == "agent-a"
    assert r.dimensions["correctness"] == pytest.approx(0.9)
    assert r.dimensions["style"] == pytest.approx(0.7)
    assert r.reasoning["correctness"] == "Good"
    assert r.confidence == pytest.approx(0.85)


@pytest.mark.asyncio
async def test_get_scores_filters_by_agent(store):
    await store.save_score(_make_score(eval_id="e1", agent="alice"))
    await store.save_score(_make_score(eval_id="e2", agent="bob"))

    since = datetime.now(UTC) - timedelta(hours=1)
    results = await store.get_scores("alice", since)
    assert len(results) == 1
    assert results[0].agent_name == "alice"


@pytest.mark.asyncio
async def test_save_and_get_override(store):
    score = _make_score()
    await store.save_score(score)

    await store.save_override("e1", {"correctness": 0.5}, "human-reviewer")

    since = datetime.now(UTC) - timedelta(hours=1)
    overrides = await store.get_overrides(since)

    assert len(overrides) == 1
    ov = overrides[0]
    assert ov["eval_id"] == "e1"
    assert ov["dimension"] == "correctness"
    assert ov["original_score"] == pytest.approx(0.9)
    assert ov["corrected_score"] == pytest.approx(0.5)
    assert ov["corrector"] == "human-reviewer"


@pytest.mark.asyncio
async def test_autonomy_default_none(store):
    level = await store.get_autonomy("unknown-agent")
    assert level is None


@pytest.mark.asyncio
async def test_set_and_get_autonomy(store):
    await store.set_autonomy("agent-a", "supervised", "admin")
    level = await store.get_autonomy("agent-a")
    assert level == "supervised"


@pytest.mark.asyncio
async def test_set_autonomy_upsert(store):
    await store.set_autonomy("agent-a", "full", "system")
    await store.set_autonomy("agent-a", "suspended", "governance")
    level = await store.get_autonomy("agent-a")
    assert level == "suspended"


@pytest.mark.asyncio
async def test_get_overrides_filtered_by_agent(store):
    await store.save_score(_make_score(eval_id="e1", agent="alice"))
    await store.save_score(_make_score(eval_id="e2", agent="bob"))

    await store.save_override("e1", {"correctness": 0.5}, "reviewer")
    await store.save_override("e2", {"correctness": 0.3}, "reviewer")

    since = datetime.now(UTC) - timedelta(hours=1)

    alice_overrides = await store.get_overrides(since, agent_name="alice")
    assert len(alice_overrides) == 1
    assert alice_overrides[0]["eval_id"] == "e1"

    bob_overrides = await store.get_overrides(since, agent_name="bob")
    assert len(bob_overrides) == 1
    assert bob_overrides[0]["eval_id"] == "e2"

    all_overrides = await store.get_overrides(since)
    assert len(all_overrides) == 2


@pytest.mark.asyncio
async def test_override_telemetry_names_the_evaluated_task(store):
    """The human grader event carries the evaluated task as its decision id.

    The store looks the task up from the evaluation (opensrm-ju8r); the
    corrector does not supply it.
    """
    from nthlayer_common.telemetry import capture_events

    await store.save_score(_make_score(eval_id="e1", task="task-42"))
    with capture_events() as cap:
        await store.save_override("e1", {"correctness": 0.5}, "reviewer")

    (event,) = cap.named("gen_ai.evaluation.result")
    assert event.attributes["nthlayer.decision.id"] == "task-42"


def _iso_ns(text: str) -> int:
    """Unix nanoseconds of a stored ``...T...Z`` timestamp."""
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return int(dt.timestamp()) * 10**9 + dt.microsecond * 1000


@pytest.mark.asyncio
async def test_override_event_time_is_the_stored_correction_time(store):
    """A1 T1: the human grader event carries when the correction was made,
    the same time the store records for it."""
    from nthlayer_common.telemetry import capture_events

    await store.save_score(_make_score(eval_id="e1"))
    with capture_events() as cap:
        await store.save_override("e1", {"correctness": 0.5, "style": 0.4}, "reviewer")

    stored = await store.get_overrides(since=datetime(2000, 1, 1, tzinfo=UTC))
    events = cap.named("gen_ai.evaluation.result")
    assert len(stored) == len(events) == 2
    assert {e.timestamp for e in events} == {_iso_ns(o["created_at"]) for o in stored}


@pytest.mark.asyncio
async def test_autonomy_event_time_is_the_stored_change_time(store):
    """A1 T1: the autonomy event carries when the level changed, the same time
    the store records on the autonomy row and in the governance log."""
    from nthlayer_common.telemetry import capture_events

    with capture_events() as cap:
        await store.set_autonomy("agent-a", "supervised", "governance")

    (event,) = cap.named("nthlayer.autonomy.changed")
    row = store._conn.execute(
        "SELECT updated_at FROM agent_autonomy WHERE agent_name = 'agent-a'"
    ).fetchone()
    log = store._conn.execute(
        "SELECT created_at FROM governance_log WHERE agent_name = 'agent-a'"
    ).fetchone()
    assert event.timestamp == _iso_ns(row["updated_at"]) == _iso_ns(log["created_at"])
