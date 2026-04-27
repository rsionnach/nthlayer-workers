"""SQLite implementation of the ScoreStore protocol."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path

from nthlayer_common.verdicts import VerdictStore as VerdictStoreBase

from nthlayer_workers.measure.telemetry import emit_override_event, emit_state_transition_event
from nthlayer_workers.measure.types import QualityScore

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


class SQLiteScoreStore:
    """Persists evaluation scores to a local SQLite database."""

    def __init__(self, db_path: str | Path, verdict_store: VerdictStoreBase | None = None) -> None:
        self._db_path = Path(db_path)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.Lock()
        self._verdict_store = verdict_store
        try:
            self._apply_schema()
            self._migrate_verdict_id()
        except Exception:
            self._conn.close()
            raise

    def _apply_schema(self) -> None:
        schema = _SCHEMA_PATH.read_text()
        with self._lock:
            self._conn.executescript(schema)

    def _migrate_verdict_id(self) -> None:
        """Add verdict_id column to evaluations if not present."""
        with self._lock:
            try:
                self._conn.execute("ALTER TABLE evaluations ADD COLUMN verdict_id TEXT")
                self._conn.commit()
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e):
                    raise

    def _save_score_sync(self, score: QualityScore) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO evaluations (eval_id, agent_name, task_id, evaluator_model, confidence, cost_usd, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    score.eval_id,
                    score.agent_name,
                    score.task_id,
                    score.evaluator_model,
                    score.confidence,
                    score.cost_usd,
                    score.timestamp.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                ),
            )
            for dim_name, dim_score in score.dimensions.items():
                reasoning = score.reasoning.get(dim_name, "")
                self._conn.execute(
                    "INSERT INTO dimension_scores (eval_id, dimension, score, reasoning) VALUES (?, ?, ?, ?)",
                    (score.eval_id, dim_name, dim_score, reasoning),
                )
            self._conn.commit()

    async def save_score(self, score: QualityScore) -> None:
        await asyncio.to_thread(self._save_score_sync, score)

    def _set_verdict_id_sync(self, eval_id: str, verdict_id: str) -> None:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE evaluations SET verdict_id = ? WHERE eval_id = ?",
                (verdict_id, eval_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Cannot set verdict_id on non-existent evaluation: {eval_id}")
            self._conn.commit()

    async def set_verdict_id(self, eval_id: str, verdict_id: str) -> None:
        await asyncio.to_thread(self._set_verdict_id_sync, eval_id, verdict_id)

    def _get_scores_sync(
        self, agent_name: str, since: datetime, limit: int
    ) -> list[QualityScore]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT e.eval_id, e.agent_name, e.task_id, e.evaluator_model, "
                "e.confidence, e.cost_usd, e.created_at, "
                "d.dimension, d.score AS dim_score, d.reasoning "
                "FROM evaluations e "
                "LEFT JOIN dimension_scores d ON e.eval_id = d.eval_id "
                "WHERE e.eval_id IN ("
                "  SELECT eval_id FROM evaluations "
                "  WHERE agent_name = ? AND created_at >= ? "
                "  ORDER BY created_at DESC LIMIT ?"
                ") ORDER BY e.created_at DESC",
                (agent_name, since.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), limit),
            ).fetchall()

        # Group rows by eval_id
        evals: dict[str, dict] = {}
        for row in rows:
            eid = row["eval_id"]
            if eid not in evals:
                evals[eid] = {
                    "eval_id": eid,
                    "agent_name": row["agent_name"],
                    "task_id": row["task_id"],
                    "evaluator_model": row["evaluator_model"],
                    "confidence": row["confidence"],
                    "cost_usd": row["cost_usd"],
                    "created_at": row["created_at"],
                    "dimensions": {},
                    "reasoning": {},
                }
            if row["dimension"] is not None:
                evals[eid]["dimensions"][row["dimension"]] = row["dim_score"]
                if row["reasoning"]:
                    evals[eid]["reasoning"][row["dimension"]] = row["reasoning"]

        results = []
        for data in evals.values():
            results.append(
                QualityScore(
                    eval_id=data["eval_id"],
                    agent_name=data["agent_name"],
                    task_id=data["task_id"],
                    dimensions=data["dimensions"],
                    reasoning=data["reasoning"],
                    confidence=data["confidence"],
                    evaluator_model=data["evaluator_model"],
                    cost_usd=data["cost_usd"],
                    timestamp=datetime.fromisoformat(data["created_at"]),
                )
            )
        return results

    async def get_scores(
        self, agent_name: str, since: datetime, limit: int = 100
    ) -> list[QualityScore]:
        return await asyncio.to_thread(self._get_scores_sync, agent_name, since, limit)

    def _save_override_sync(
        self, eval_id: str, corrected_dimensions: dict[str, float], corrector: str
    ) -> None:
        verdict_id = None
        with self._lock:
            # Verify eval_id exists
            exists = self._conn.execute(
                "SELECT 1 FROM evaluations WHERE eval_id = ?", (eval_id,)
            ).fetchone()
            if not exists:
                raise ValueError(
                    f"Cannot override non-existent evaluation: {eval_id}"
                )

            for dim_name, corrected_score in corrected_dimensions.items():
                row = self._conn.execute(
                    "SELECT score FROM dimension_scores "
                    "WHERE eval_id = ? AND dimension = ?",
                    (eval_id, dim_name),
                ).fetchone()
                original_score = row["score"] if row else 0.0
                self._conn.execute(
                    "INSERT INTO overrides "
                    "(override_id, eval_id, dimension, original_score, "
                    "corrected_score, corrector) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        str(uuid.uuid4()),
                        eval_id,
                        dim_name,
                        original_score,
                        corrected_score,
                        corrector,
                    ),
                )
                emit_override_event(
                    eval_id, dim_name, original_score,
                    corrected_score, corrector,
                )
            self._conn.commit()

            # Look up verdict_id for resolution
            if self._verdict_store is not None:
                verdict_row = self._conn.execute(
                    "SELECT verdict_id FROM evaluations WHERE eval_id = ?",
                    (eval_id,),
                ).fetchone()
                verdict_id = (
                    verdict_row["verdict_id"] if verdict_row else None
                )

        # Resolve linked verdict outside the lock (verdict store has its own)
        if self._verdict_store is not None and verdict_id is not None:
            self._verdict_store.resolve(
                verdict_id, "overridden", override={"by": corrector},
            )

    async def save_override(
        self, eval_id: str, corrected_dimensions: dict[str, float], corrector: str
    ) -> None:
        await asyncio.to_thread(self._save_override_sync, eval_id, corrected_dimensions, corrector)

    def _get_overrides_sync(
        self, since: datetime, limit: int, agent_name: str | None = None
    ) -> list[dict]:
        with self._lock:
            if agent_name is not None:
                rows = self._conn.execute(
                    "SELECT o.override_id, o.eval_id, o.dimension, o.original_score, "
                    "o.corrected_score, o.corrector, o.created_at "
                    "FROM overrides o JOIN evaluations e ON o.eval_id = e.eval_id "
                    "WHERE o.created_at >= ? AND e.agent_name = ? "
                    "ORDER BY o.created_at DESC LIMIT ?",
                    (since.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), agent_name, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT override_id, eval_id, dimension, original_score, corrected_score, corrector, created_at "
                    "FROM overrides WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
                    (since.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), limit),
                ).fetchall()
            return [dict(r) for r in rows]

    async def get_overrides(
        self, since: datetime, limit: int = 100, agent_name: str | None = None
    ) -> list[dict]:
        return await asyncio.to_thread(self._get_overrides_sync, since, limit, agent_name)

    def _get_autonomy_sync(self, agent_name: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT level FROM agent_autonomy WHERE agent_name = ?",
                (agent_name,),
            ).fetchone()
            return row["level"] if row else None

    async def get_autonomy(self, agent_name: str) -> str | None:
        return await asyncio.to_thread(self._get_autonomy_sync, agent_name)

    def _set_autonomy_sync(self, agent_name: str, level: str, updated_by: str) -> None:
        with self._lock:
            current = self._conn.execute(
                "SELECT level FROM agent_autonomy WHERE agent_name = ?",
                (agent_name,),
            ).fetchone()
            from_level = current["level"] if current else "full"

            self._conn.execute(
                "INSERT INTO agent_autonomy (agent_name, level, updated_by) VALUES (?, ?, ?) "
                "ON CONFLICT(agent_name) DO UPDATE SET level = excluded.level, "
                "updated_by = excluded.updated_by, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
                (agent_name, level, updated_by),
            )
            self._conn.execute(
                "INSERT INTO governance_log (agent_name, from_level, to_level, reason, triggered_by) "
                "VALUES (?, ?, ?, ?, ?)",
                (agent_name, from_level, level, f"Autonomy changed to {level}", updated_by),
            )
            self._conn.commit()
        emit_state_transition_event(agent_name, from_level, level, updated_by)

    async def set_autonomy(self, agent_name: str, level: str, updated_by: str) -> None:
        await asyncio.to_thread(self._set_autonomy_sync, agent_name, level, updated_by)

    def close(self) -> None:
        self._conn.close()
