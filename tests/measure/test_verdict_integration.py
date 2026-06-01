"""Tests for Arbiter / Verdict integration (Phase 1)."""

from __future__ import annotations

import argparse as _argparse
import asyncio
import contextlib
import io
import json
import textwrap
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from nthlayer_common.verdicts import AccuracyFilter, SQLiteVerdictStore, VerdictFilter
from nthlayer_common.verdicts import create as verdict_create

from nthlayer_workers.measure.calibration.verdict_calibration import VerdictCalibration
from nthlayer_workers.measure.cli import _build_pipeline, cmd_calibrate
from nthlayer_workers.measure.config import MeasureConfig, VerdictConfig, load_config
from nthlayer_workers.measure.pipeline.router import DEFAULT_APPROVE_THRESHOLD, PipelineRouter
from nthlayer_workers.measure.store.sqlite import SQLiteScoreStore
from nthlayer_workers.measure.types import QualityScore


class TestVerdictConfig:
    """Tests for VerdictConfig dataclass and config loading."""

    def test_verdict_config_defaults(self):
        vc = VerdictConfig()
        assert vc.store_path == "verdicts.db"

    def test_verdict_config_custom_path(self):
        vc = VerdictConfig(store_path="/tmp/custom.db")
        assert vc.store_path == "/tmp/custom.db"

    def test_arbiter_config_verdict_none_by_default(self):
        config = MeasureConfig()
        assert config.verdict is None

    def test_load_config_without_verdict_section(self, tmp_path):
        cfg_file = tmp_path / "arbiter.yaml"
        cfg_file.write_text(textwrap.dedent("""\
            evaluator:
              model: test-model
        """))
        config = load_config(cfg_file)
        assert config.verdict is None

    def test_load_config_with_verdict_section(self, tmp_path):
        cfg_file = tmp_path / "arbiter.yaml"
        cfg_file.write_text(textwrap.dedent("""\
            evaluator:
              model: test-model
            verdict:
              store:
                path: custom-verdicts.db
        """))
        config = load_config(cfg_file)
        assert config.verdict is not None
        assert config.verdict.store_path == "custom-verdicts.db"

    def test_load_config_with_verdict_section_defaults(self, tmp_path):
        cfg_file = tmp_path / "arbiter.yaml"
        cfg_file.write_text(textwrap.dedent("""\
            evaluator:
              model: test-model
            verdict:
              store: {}
        """))
        config = load_config(cfg_file)
        assert config.verdict is not None
        assert config.verdict.store_path == "verdicts.db"


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


class TestSchemaMigration:
    """Tests for verdict_id column migration and set_verdict_id."""

    @pytest_asyncio.fixture
    async def store(self, tmp_path):
        s = SQLiteScoreStore(tmp_path / "test.db")
        yield s
        s.close()

    @pytest.mark.asyncio
    async def test_verdict_id_column_exists_after_init(self, store):
        """The evaluations table should have a verdict_id column after init."""
        with store._lock:
            row = store._conn.execute(
                "PRAGMA table_info(evaluations)"
            ).fetchall()
        col_names = [r["name"] for r in row]
        assert "verdict_id" in col_names

    @pytest.mark.asyncio
    async def test_verdict_id_null_by_default(self, store):
        """New evaluations should have verdict_id = NULL."""
        await store.save_score(_make_score())
        with store._lock:
            row = store._conn.execute(
                "SELECT verdict_id FROM evaluations WHERE eval_id = ?", ("e1",)
            ).fetchone()
        assert row["verdict_id"] is None

    @pytest.mark.asyncio
    async def test_set_verdict_id(self, store):
        """set_verdict_id should update the verdict_id for a given eval_id."""
        await store.save_score(_make_score())
        await store.set_verdict_id("e1", "vrd-2026-03-13-abcd1234-00001")
        with store._lock:
            row = store._conn.execute(
                "SELECT verdict_id FROM evaluations WHERE eval_id = ?", ("e1",)
            ).fetchone()
        assert row["verdict_id"] == "vrd-2026-03-13-abcd1234-00001"

    @pytest.mark.asyncio
    async def test_set_verdict_id_unknown_raises(self, store):
        """set_verdict_id on non-existent eval_id should raise ValueError."""
        with pytest.raises(ValueError, match="non-existent"):
            await store.set_verdict_id("no-such-id", "vrd-xxx")

    @pytest.mark.asyncio
    async def test_migration_idempotent(self, tmp_path):
        """Creating SQLiteScoreStore twice on the same DB should not crash."""
        db = tmp_path / "test.db"
        s1 = SQLiteScoreStore(db)
        s1.close()
        s2 = SQLiteScoreStore(db)
        s2.close()
        # No exception means migration is idempotent


class TestVerdictEmission:
    """Tests for verdict creation in PipelineRouter."""

    @pytest_asyncio.fixture
    async def verdict_store(self, tmp_path):
        vs = SQLiteVerdictStore(str(tmp_path / "verdicts.db"))
        yield vs
        vs.close()

    @pytest_asyncio.fixture
    async def score_store(self, tmp_path):
        s = SQLiteScoreStore(tmp_path / "score.db")
        yield s
        s.close()

    def _make_pipeline(self, score_store, verdict_store=None, threshold=None):
        """Build a PipelineRouter with mock adapter/evaluator for testing."""
        adapter = AsyncMock()
        evaluator_mock = AsyncMock()
        tracker = AsyncMock()
        tracker.compute_window = AsyncMock()

        return PipelineRouter(
            adapter=adapter,
            evaluator=evaluator_mock,
            store=score_store,
            tracker=tracker,
            dimensions=["correctness", "style"],
            verdict_store=verdict_store,
            approve_threshold=threshold,
        )

    async def _run_single(self, router, score):
        """Configure adapter to yield one output, evaluator to return score, run pipeline."""
        output = MagicMock()
        output.agent_name = score.agent_name

        async def _receive():
            yield output

        router._adapter.receive = _receive
        router._evaluator.evaluate = AsyncMock(return_value=score)
        await router.run()

    @pytest.mark.asyncio
    async def test_verdict_created_after_scoring(self, score_store, verdict_store):
        router = self._make_pipeline(score_store, verdict_store)
        score = _make_score(dimensions={"correctness": 0.8, "style": 0.6})
        await self._run_single(router, score)

        # Verdict should be in verdict store
        verdicts = verdict_store.query(VerdictFilter(producer_system="arbiter", limit=10))
        assert len(verdicts) == 1
        v = verdicts[0]
        assert v.subject.type == "agent_output"
        assert v.subject.ref == "t1"
        assert v.subject.agent == "agent-a"
        assert v.judgment.action == "approve"  # avg 0.7 >= 0.5
        assert v.judgment.confidence == pytest.approx(0.85)
        assert v.judgment.score == pytest.approx(0.7)
        assert v.judgment.dimensions == {"correctness": 0.8, "style": 0.6}
        assert v.subject.summary == "Evaluation of agent-a: t1"
        assert v.producer.system == "arbiter"
        assert v.producer.model == "test-model"
        assert v.metadata.cost_currency == pytest.approx(0.01)

    @pytest.mark.asyncio
    async def test_verdict_id_written_to_evaluations(self, score_store, verdict_store):
        router = self._make_pipeline(score_store, verdict_store)
        score = _make_score()
        await self._run_single(router, score)

        # verdict_id should be set on the evaluations row
        with score_store._lock:
            row = score_store._conn.execute(
                "SELECT verdict_id FROM evaluations WHERE eval_id = ?", ("e1",)
            ).fetchone()
        assert row["verdict_id"] is not None
        assert row["verdict_id"].startswith("vrd-")

    @pytest.mark.asyncio
    async def test_approve_threshold_boundary_approve(self, score_store, verdict_store):
        """Score exactly at threshold -> approve."""
        router = self._make_pipeline(score_store, verdict_store)
        score = _make_score(dimensions={"d1": 0.5})
        await self._run_single(router, score)

        verdicts = verdict_store.query(VerdictFilter(producer_system="arbiter", limit=10))
        assert verdicts[0].judgment.action == "approve"

    @pytest.mark.asyncio
    async def test_approve_threshold_boundary_reject(self, score_store, verdict_store):
        """Score just below threshold -> reject."""
        router = self._make_pipeline(score_store, verdict_store)
        score = _make_score(dimensions={"d1": 0.49})
        await self._run_single(router, score)

        verdicts = verdict_store.query(VerdictFilter(producer_system="arbiter", limit=10))
        assert verdicts[0].judgment.action == "reject"

    @pytest.mark.asyncio
    async def test_custom_threshold(self, score_store, verdict_store):
        """Custom approve_threshold should be respected."""
        router = self._make_pipeline(score_store, verdict_store, threshold=0.8)
        score = _make_score(dimensions={"d1": 0.75})
        await self._run_single(router, score)

        verdicts = verdict_store.query(VerdictFilter(producer_system="arbiter", limit=10))
        assert verdicts[0].judgment.action == "reject"  # 0.75 < 0.8

    @pytest.mark.asyncio
    async def test_no_verdict_when_store_is_none(self, score_store):
        """When verdict_store is None, pipeline works without creating verdicts."""
        router = self._make_pipeline(score_store, verdict_store=None)
        score = _make_score()
        await self._run_single(router, score)

        # Score still saved
        since = datetime.now(UTC) - timedelta(hours=1)
        results = await score_store.get_scores("agent-a", since)
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_verdict_reasoning_formatted(self, score_store, verdict_store):
        """Reasoning dict should be formatted as semicolon-separated string."""
        router = self._make_pipeline(score_store, verdict_store)
        score = _make_score(
            reasoning={"correctness": "Looks good", "style": "Needs work"}
        )
        await self._run_single(router, score)

        verdicts = verdict_store.query(VerdictFilter(producer_system="arbiter", limit=10))
        reasoning = verdicts[0].judgment.reasoning
        assert "correctness: Looks good" in reasoning
        assert "style: Needs work" in reasoning

    @pytest.mark.asyncio
    async def test_default_approve_threshold_value(self):
        assert DEFAULT_APPROVE_THRESHOLD == 0.5


class TestOverrideResolution:
    """Tests for override to verdict resolution in SQLiteScoreStore."""

    @pytest_asyncio.fixture
    async def verdict_store(self, tmp_path):
        vs = SQLiteVerdictStore(str(tmp_path / "verdicts.db"))
        yield vs
        vs.close()

    @pytest_asyncio.fixture
    async def score_store(self, tmp_path, verdict_store):
        s = SQLiteScoreStore(tmp_path / "score.db", verdict_store=verdict_store)
        yield s
        s.close()

    @pytest.mark.asyncio
    async def test_override_resolves_verdict(self, score_store, verdict_store):
        """Override should resolve the linked verdict as overridden."""
        # Save score
        await score_store.save_score(_make_score())

        # Create and store verdict, link it
        verdict = await asyncio.to_thread(
            verdict_create,
            subject={"type": "agent_output", "ref": "t1", "agent": "agent-a",
                     "summary": "Test evaluation"},
            judgment={"action": "approve", "confidence": 0.85, "score": 0.8},
            producer={"system": "arbiter", "model": "test-model"},
        )
        await asyncio.to_thread(verdict_store.put, verdict)
        await score_store.set_verdict_id("e1", verdict.id)

        # Override
        await score_store.save_override("e1", {"correctness": 0.3}, "human-reviewer")

        # Verdict should be resolved
        resolved = verdict_store.get(verdict.id)
        assert resolved.outcome.status == "overridden"
        assert resolved.outcome.override.by == "human-reviewer"

    @pytest.mark.asyncio
    async def test_override_without_verdict_id_still_works(self, score_store):
        """Override on pre-integration data (no verdict_id) should work normally."""
        await score_store.save_score(_make_score())
        # No verdict_id set — simulates pre-integration data
        await score_store.save_override("e1", {"correctness": 0.3}, "reviewer")

        since = datetime.now(UTC) - timedelta(hours=1)
        overrides = await score_store.get_overrides(since)
        assert len(overrides) == 1

    @pytest.mark.asyncio
    async def test_score_store_without_verdict_store(self, tmp_path):
        """SQLiteScoreStore without verdict_store should work identically to before."""
        s = SQLiteScoreStore(tmp_path / "test.db")
        try:
            await s.save_score(_make_score())
            await s.save_override("e1", {"correctness": 0.3}, "reviewer")
            since = datetime.now(UTC) - timedelta(hours=1)
            overrides = await s.get_overrides(since)
            assert len(overrides) == 1
        finally:
            s.close()


class TestVerdictCalibration:
    """Tests for VerdictCalibration — system-wide accuracy via verdict store."""

    @pytest_asyncio.fixture
    async def verdict_store(self, tmp_path):
        vs = SQLiteVerdictStore(str(tmp_path / "verdicts.db"))
        yield vs
        vs.close()

    def _seed_verdicts(self, verdict_store, count=5, overridden_count=1):
        """Create verdicts: confirmed + overridden to given counts."""
        for i in range(count):
            v = verdict_create(
                subject={
                    "type": "agent_output",
                    "ref": f"task-{i}",
                    "agent": "agent-a",
                    "summary": f"Test evaluation {i}",
                },
                judgment={
                    "action": "approve",
                    "confidence": 0.9,
                    "score": 0.8,
                },
                producer={"system": "arbiter", "model": "test-model"},
            )
            verdict_store.put(v)
            if i < overridden_count:
                verdict_store.resolve(
                    v.id, "overridden", override={"by": "human"},
                )
            else:
                verdict_store.resolve(v.id, "confirmed")

    @pytest.mark.asyncio
    async def test_check_returns_accuracy_report(self, verdict_store):
        self._seed_verdicts(verdict_store, count=5, overridden_count=1)
        cal = VerdictCalibration(verdict_store)
        report = await cal.check()

        assert report.total == 5
        assert report.total_resolved == 5
        # 4 confirmed / 5 resolved = 0.8
        assert report.confirmation_rate == pytest.approx(0.8)
        # 1 overridden / 5 resolved = 0.2
        assert report.override_rate == pytest.approx(0.2)

    @pytest.mark.asyncio
    async def test_check_empty_store(self, verdict_store):
        cal = VerdictCalibration(verdict_store)
        report = await cal.check()

        assert report.total == 0
        assert report.total_resolved == 0
        assert report.confirmation_rate == 0.0
        assert report.override_rate == 0.0

    @pytest.mark.asyncio
    async def test_check_custom_window(self, verdict_store):
        self._seed_verdicts(verdict_store, count=3, overridden_count=0)
        cal = VerdictCalibration(verdict_store)
        report = await cal.check(window_days=1)

        assert report.total == 3
        assert report.confirmation_rate == pytest.approx(1.0)


class TestCalibrateVerdictFlag:
    """Tests for --verdict flag on calibrate subcommand."""

    @pytest_asyncio.fixture
    async def verdict_db(self, tmp_path):
        """Create a seeded verdict store and return the db path."""
        db_path = str(tmp_path / "verdicts.db")
        vs = SQLiteVerdictStore(db_path)
        # Seed: 4 confirmed, 1 overridden
        for i in range(5):
            v = verdict_create(
                subject={
                    "type": "agent_output",
                    "ref": f"t-{i}",
                    "agent": "a",
                    "summary": f"Test {i}",
                },
                judgment={
                    "action": "approve",
                    "confidence": 0.9,
                    "score": 0.8,
                },
                producer={"system": "arbiter", "model": "test"},
            )
            vs.put(v)
            if i == 0:
                vs.resolve(v.id, "overridden", override={"by": "human"})
            else:
                vs.resolve(v.id, "confirmed")
        vs.close()
        return db_path

    def _make_config_file(self, tmp_path, verdict_store_path):
        """Write a minimal arbiter.yaml with verdict config."""
        cfg = tmp_path / "arbiter.yaml"
        cfg.write_text(
            f"evaluator:\n  model: test\n"
            f"store:\n  path: {tmp_path / 'arbiter.db'}\n"
            f"verdict:\n  store:\n    path: {verdict_store_path}\n"
        )
        return cfg

    def test_calibrate_verdict_flag_prints_report(self, tmp_path, verdict_db):
        cfg_file = self._make_config_file(tmp_path, verdict_db)

        args = _argparse.Namespace(
            config=cfg_file,
            agent=None,
            window_days=30,
            verdict=True,
        )

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_calibrate(args)
        output = buf.getvalue()
        result = json.loads(output)

        assert result["total"] == 5
        assert result["total_resolved"] == 5
        assert result["confirmation_rate"] == pytest.approx(0.8)
        assert result["override_rate"] == pytest.approx(0.2)

    def test_calibrate_verdict_flag_no_config_errors(self, tmp_path):
        """--verdict without verdict config section should print error."""
        cfg = tmp_path / "arbiter.yaml"
        cfg.write_text("evaluator:\n  model: test\n")
        args = _argparse.Namespace(
            config=cfg,
            agent=None,
            window_days=30,
            verdict=True,
        )

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with pytest.raises(SystemExit):
                cmd_calibrate(args)
        assert "verdict" in buf.getvalue().lower()


class TestEndToEndFeedbackLoop:
    """End-to-end test: score -> verdict -> override -> resolution -> accuracy."""

    @pytest_asyncio.fixture
    async def stores(self, tmp_path):
        verdict_store = SQLiteVerdictStore(str(tmp_path / "verdicts.db"))
        score_store = SQLiteScoreStore(
            tmp_path / "scores.db", verdict_store=verdict_store,
        )
        yield score_store, verdict_store
        score_store.close()
        verdict_store.close()

    @pytest.mark.asyncio
    async def test_full_feedback_loop(self, stores):
        score_store, verdict_store = stores

        # 1. Three agent outputs scored through the pipeline
        scores = [
            _make_score(
                eval_id="e1", agent="coder", task="pr-101",
                dimensions={"correctness": 0.9, "safety": 0.8},
                confidence=0.9,
            ),
            _make_score(
                eval_id="e2", agent="coder", task="pr-102",
                dimensions={"correctness": 0.7, "safety": 0.6},
                confidence=0.7,
            ),
            _make_score(
                eval_id="e3", agent="coder", task="pr-103",
                dimensions={"correctness": 0.3, "safety": 0.2},
                confidence=0.5,
            ),
        ]

        router = PipelineRouter(
            adapter=AsyncMock(),
            evaluator=AsyncMock(),
            store=score_store,
            tracker=AsyncMock(),
            dimensions=["correctness", "safety"],
            verdict_store=verdict_store,
        )

        for score in scores:
            output = MagicMock()
            output.agent_name = score.agent_name

            async def _receive(o=output):
                yield o

            router._adapter.receive = _receive
            router._evaluator.evaluate = AsyncMock(return_value=score)
            await router.run()

        # 2. Verify three verdicts exist
        verdicts = verdict_store.query(
            VerdictFilter(producer_system="arbiter", limit=0),
        )
        assert len(verdicts) == 3

        # e1 avg=0.85 -> approve, e2 avg=0.65 -> approve, e3 avg=0.25 -> reject
        actions = sorted([v.judgment.action for v in verdicts])
        assert actions == ["approve", "approve", "reject"]

        # 3. Check accuracy before any overrides — all pending
        report_before = verdict_store.accuracy(
            AccuracyFilter(producer_system="arbiter"),
        )
        assert report_before.total == 3
        assert report_before.total_resolved == 0
        assert report_before.pending_rate == pytest.approx(1.0)

        # 4. Override e1 — human disagrees with the approve
        await score_store.save_override(
            "e1", {"correctness": 0.3}, "senior-reviewer",
        )

        # 5. Confirm e2 and e3 manually (resolve their verdicts)
        for v in verdicts:
            if v.subject.ref in ("pr-102", "pr-103"):
                verdict_store.resolve(v.id, "confirmed")

        # 6. Check accuracy after resolutions
        report_after = verdict_store.accuracy(
            AccuracyFilter(producer_system="arbiter"),
        )
        assert report_after.total == 3
        assert report_after.total_resolved == 3
        # 2 confirmed, 1 overridden
        assert report_after.confirmation_rate == pytest.approx(2 / 3, abs=0.01)
        assert report_after.override_rate == pytest.approx(1 / 3, abs=0.01)

        # 7. VerdictCalibration should report the same
        cal = VerdictCalibration(verdict_store)
        cal_report = await cal.check()
        assert cal_report.total_resolved == 3
        assert cal_report.override_rate == pytest.approx(1 / 3, abs=0.01)


class TestOverrideCreateCLI:
    """Tests for `arbiter overrides create` CLI subcommand."""

    def _seed_score(self, tmp_path, with_verdict=False):
        """Create a store with one score and return config_path."""
        store = SQLiteScoreStore(tmp_path / "arbiter.db")
        score = _make_score(eval_id="eval-1", agent="coder", task="pr-1")
        asyncio.run(store.save_score(score))

        verdict_line = ""
        if with_verdict:
            verdict_line = (
                f"verdict:\n  store:\n    path: {tmp_path / 'verdicts.db'}\n"
            )

        cfg = tmp_path / "arbiter.yaml"
        cfg.write_text(
            f"evaluator:\n  model: test\n"
            f"store:\n  path: {tmp_path / 'arbiter.db'}\n"
            f"{verdict_line}"
        )
        store.close()
        return cfg

    def test_override_create_saves_and_prints(self, tmp_path):
        from nthlayer_workers.measure.cli import cmd_overrides_create

        cfg = self._seed_score(tmp_path)
        args = _argparse.Namespace(
            config=cfg,
            eval_id="eval-1",
            corrector="human:rob",
            dimension=["correctness=0.4"],
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_overrides_create(args)

        result = json.loads(buf.getvalue())
        assert result["eval_id"] == "eval-1"
        assert result["corrector"] == "human:rob"
        assert result["corrected_dimensions"] == {"correctness": 0.4}

    def test_override_create_multiple_dimensions(self, tmp_path):
        from nthlayer_workers.measure.cli import cmd_overrides_create

        cfg = self._seed_score(tmp_path)
        args = _argparse.Namespace(
            config=cfg,
            eval_id="eval-1",
            corrector="human:rob",
            dimension=["correctness=0.4", "style=0.2"],
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_overrides_create(args)

        result = json.loads(buf.getvalue())
        assert result["corrected_dimensions"] == {"correctness": 0.4, "style": 0.2}

    def test_override_create_resolves_linked_verdict(self, tmp_path):
        """Override via CLI should resolve the linked verdict when verdict store is configured."""
        from nthlayer_workers.measure.cli import cmd_overrides_create

        # Seed score, create verdict, link them
        verdict_store = SQLiteVerdictStore(str(tmp_path / "verdicts.db"))
        store = SQLiteScoreStore(tmp_path / "arbiter.db", verdict_store=verdict_store)
        score = _make_score(eval_id="eval-1", agent="coder", task="pr-1")
        asyncio.run(store.save_score(score))
        v = verdict_create(
            subject={"type": "agent_output", "ref": "pr-1", "agent": "coder",
                     "summary": "test"},
            judgment={"action": "approve", "confidence": 0.9, "score": 0.8},
            producer={"system": "arbiter", "model": "test"},
        )
        verdict_store.put(v)
        asyncio.run(store.set_verdict_id("eval-1", v.id))
        store.close()
        verdict_store.close()

        # Write config pointing at the already-seeded databases
        cfg = tmp_path / "arbiter.yaml"
        cfg.write_text(
            f"evaluator:\n  model: test\n"
            f"store:\n  path: {tmp_path / 'arbiter.db'}\n"
            f"verdict:\n  store:\n    path: {tmp_path / 'verdicts.db'}\n"
        )

        args = _argparse.Namespace(
            config=cfg,
            eval_id="eval-1",
            corrector="human:rob",
            dimension=["correctness=0.4"],
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_overrides_create(args)

        # Verify verdict was resolved
        vs2 = SQLiteVerdictStore(str(tmp_path / "verdicts.db"))
        resolved = vs2.get(v.id)
        assert resolved.outcome.status == "overridden"
        vs2.close()

    def test_override_create_bad_dimension_format(self, tmp_path):
        from nthlayer_workers.measure.cli import cmd_overrides_create

        cfg = self._seed_score(tmp_path)
        args = _argparse.Namespace(
            config=cfg,
            eval_id="eval-1",
            corrector="human:rob",
            dimension=["bad-format"],
        )
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with pytest.raises(SystemExit):
                cmd_overrides_create(args)
        assert "dimension" in buf.getvalue().lower()


class TestCLIVerdictWiring:
    """Tests that CLI wires verdict store when config has verdict section."""

    def test_build_pipeline_includes_verdict_store(self, tmp_path):
        from nthlayer_workers.measure.config import MeasureConfig, VerdictConfig

        config = MeasureConfig(
            verdict=VerdictConfig(
                store_path=str(tmp_path / "verdicts.db"),
            ),
        )
        config.store.path = str(tmp_path / "arbiter.db")

        router = _build_pipeline(config)
        assert router._verdict_store is not None

    def test_build_pipeline_no_verdict_config(self, tmp_path):
        from nthlayer_workers.measure.config import MeasureConfig

        config = MeasureConfig()
        config.store.path = str(tmp_path / "arbiter.db")

        router = _build_pipeline(config)
        assert router._verdict_store is None
