# src/nthlayer_respond/safe_actions/registry.py
"""Safe action registry — closed callable registry with cooldown persistence."""
from __future__ import annotations

import inspect
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

import structlog

from nthlayer_workers.respond.types import IncidentContext

logger = structlog.get_logger(__name__)


@dataclass
class SafeAction:
    """A pre-approved action the remediation agent may execute."""

    name: str
    description: str              # included in remediation agent's prompt
    target_type: str              # "service", "agent", "feature_flag", "arbiter"
    requires_approval: bool       # model can escalate, never downgrade
    cooldown_seconds: int
    handler: Callable             # async (target, context, **kwargs) -> dict
    blast_radius_check: Callable | None = None  # (target, topology_dict) -> bool


class SafeActionRegistry:
    """Closed registry of safe actions with SQLite-backed cooldown tracking.

    Actions are registered at startup.  Unknown names fail with KeyError at
    both get() and execute() time — no runtime eval of arbitrary names.
    """

    def __init__(self, cooldown_store_path: str) -> None:
        self._actions: dict[str, SafeAction] = {}
        self._db_path = cooldown_store_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(cooldown_store_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init_db()

    # ------------------------------------------------------------------ #
    # DB                                                                   #
    # ------------------------------------------------------------------ #

    def _init_db(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cooldown_log (
                    action_name TEXT NOT NULL,
                    target       TEXT NOT NULL,
                    executed_at  REAL NOT NULL,
                    PRIMARY KEY (action_name, target)
                )
                """
            )
            self._conn.commit()

    def _record_execution(self, name: str, target: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO cooldown_log (action_name, target, executed_at)
                VALUES (?, ?, ?)
                ON CONFLICT(action_name, target) DO UPDATE SET executed_at=excluded.executed_at
                """,
                (name, target, time.time()),
            )
            self._conn.commit()

    def _last_executed_at(self, name: str, target: str) -> float | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT executed_at FROM cooldown_log WHERE action_name=? AND target=?",
                (name, target),
            ).fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def register(self, action: SafeAction) -> None:
        """Register an action at startup."""
        self._actions[action.name] = action
        logger.debug("Registered safe action", action_name=action.name)

    def get(self, name: str) -> SafeAction:
        """Return the action by name; raises KeyError if not registered."""
        try:
            return self._actions[name]
        except KeyError:
            raise KeyError(f"Unknown safe action: {name!r}") from None

    def list_actions(self) -> list[dict]:
        """Return [{name, description}, ...] for use in agent prompts."""
        return [
            {"name": a.name, "description": a.description}
            for a in self._actions.values()
        ]

    def check_cooldown(self, name: str, target: str) -> bool:
        """Return True if the cooldown period has elapsed (safe to execute)."""
        action = self.get(name)
        if action.cooldown_seconds == 0:
            return True
        last = self._last_executed_at(name, target)
        if last is None:
            return True
        elapsed = time.time() - last
        return elapsed >= action.cooldown_seconds

    async def execute(
        self,
        name: str,
        target: str,
        context: IncidentContext,
        **kwargs,
    ) -> dict:
        """Validate, check cooldown, check blast radius, call handler, log.

        Returns {"success": bool, "detail": str, "timestamp": str}.
        Raises KeyError for unknown action names.
        Raises RuntimeError when cooldown or blast radius check fails.
        """
        action = self.get(name)  # KeyError propagates

        # Cooldown check
        if not self.check_cooldown(name, target):
            action_obj = self._actions[name]
            raise RuntimeError(
                f"Action {name!r} on target {target!r} is in cooldown "
                f"({action_obj.cooldown_seconds}s between executions)."
            )

        # Blast radius check
        if action.blast_radius_check is not None:
            allowed = action.blast_radius_check(target, context)
            if not allowed:
                raise RuntimeError(
                    f"blast radius check failed for action {name!r} on target {target!r}. "
                    "Action blocked to prevent over-broad impact."
                )

        # Call handler (supports both sync and async handlers)
        if inspect.iscoroutinefunction(action.handler):
            result = await action.handler(target, context, **kwargs)
        else:
            result = action.handler(target, context, **kwargs)

        # Record execution for cooldown tracking
        self._record_execution(name, target)

        timestamp = datetime.now(timezone.utc).isoformat()
        logger.info(
            "Executed safe action %r on target %r at %s — success=%s",
            name, target, timestamp, result.get("success"),
        )

        return {
            "success": result.get("success", False),
            "detail": result.get("detail", ""),
            "timestamp": timestamp,
        }
