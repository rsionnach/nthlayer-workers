"""Governance engine — watches error budgets, manages agent autonomy.

Key constraint: can REDUCE autonomy, never increase without human approval
(one-way safety ratchet). Delegates governance judgment to the model (ZFC).
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Protocol

from nthlayer_common.prompts import load_prompt, render_user_prompt

from nthlayer_workers.measure.store.protocol import ScoreStore
from nthlayer_workers.measure.trends.tracker import TrendTracker
from nthlayer_workers.measure.types import AutonomyLevel, GovernanceAction, TrendWindow

_PROMPT_PATH = Path(__file__).parent.parent.parent.parent / "prompts" / "governance.yaml"

logger = logging.getLogger(__name__)


class GovernanceEngine(Protocol):
    """Manages agent autonomy levels based on evaluation trends."""

    async def check_agent(self, agent_name: str) -> GovernanceAction | None: ...

    async def get_autonomy(self, agent_name: str) -> AutonomyLevel: ...

    async def restore_autonomy(self, agent_name: str, level: AutonomyLevel, approver: str) -> None:
        """Restore autonomy — requires human approver (safety ratchet)."""
        ...


class ErrorBudgetGovernance:
    """Governance based on error budget consumption over a rolling window.

    The governance DECISION is judgment — it goes to the model (ZFC).
    The governance ACTION (persisting reduced autonomy) is transport — it stays in code.
    """

    def __init__(
        self,
        store: ScoreStore,
        tracker: TrendTracker,
        window_days: int = 7,
        threshold: float = 0.5,
        model: str | None = None,
        max_tokens: int = 1024,
    ) -> None:
        self._store = store
        self._tracker = tracker
        self._window_days = window_days
        self._threshold = threshold
        self._model = model
        self._max_tokens = max_tokens

    def build_governance_prompt(
        self, agent_name: str, trend: TrendWindow, current_level: AutonomyLevel
    ) -> str:
        """Construct the governance judgment prompt from YAML template."""
        spec = load_prompt(_PROMPT_PATH)
        dims = "\n".join(
            f"  - {name}: {avg:.3f}" for name, avg in trend.dimension_averages.items()
        )
        return render_user_prompt(
            spec.user_template,
            agent_name=agent_name,
            window_days=str(trend.window_days),
            evaluation_count=str(trend.evaluation_count),
            confidence_mean=f"{trend.confidence_mean:.3f}",
            reversal_rate=f"{trend.reversal_rate:.3f}",
            current_level=current_level.value,
            dimension_averages=dims,
            threshold=str(self._threshold),
            reduced_level=self._reduce_level(current_level).value,
        )

    def parse_governance_response(self, raw: str) -> tuple[bool, str]:
        """Parse model governance response. Returns (should_reduce, reason)."""
        from nthlayer_workers.measure._parsing import strip_markdown_fences

        text = strip_markdown_fences(raw)
        data = json.loads(text)
        return bool(data.get("should_reduce", False)), str(data.get("reason", ""))

    async def check_agent(self, agent_name: str) -> GovernanceAction | None:
        trend = await self._tracker.compute_window(agent_name, self._window_days)

        if trend.evaluation_count == 0:
            return None

        # ZFC: governance judgment requires a model. No model → no opinion.
        if self._model is None:
            logger.debug("No governance model configured, skipping judgment for %s", agent_name)
            return None

        current = await self.get_autonomy(agent_name)
        reduced = self._reduce_level(current)
        if reduced == current:
            return None  # Already at lowest level

        try:
            from nthlayer_common.llm import llm_call

            prompt = self.build_governance_prompt(agent_name, trend, current)
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    llm_call,
                    system="",
                    user=prompt,
                    model=self._model,
                    max_tokens=self._max_tokens,
                    timeout=60,
                ),
                timeout=60.0,
            )
            if not result.text:
                logger.warning("Governance model returned empty content for %s", agent_name)
                return None
            should_reduce, reason = self.parse_governance_response(result.text)
        except Exception:
            # ZFC: fail open on model unavailability — no governance opinion
            logger.warning("Governance model call failed for %s, failing open", agent_name, exc_info=True)
            return None

        if not should_reduce:
            return None

        await self._store.set_autonomy(
            agent_name, reduced.value, "governance:error_budget"
        )
        return GovernanceAction(
            agent_name=agent_name,
            target_level=reduced,
            reason=reason,
        )

    async def get_autonomy(self, agent_name: str) -> AutonomyLevel:
        level_str = await self._store.get_autonomy(agent_name)
        if level_str is None:
            return AutonomyLevel.FULL
        return AutonomyLevel(level_str)

    async def restore_autonomy(
        self, agent_name: str, level: AutonomyLevel, approver: str
    ) -> None:
        if not approver:
            raise ValueError("Safety ratchet: approver is required to restore autonomy")
        await self._store.set_autonomy(agent_name, level.value, approver)

    @staticmethod
    def _reduce_level(current: AutonomyLevel) -> AutonomyLevel:
        """One step down the autonomy ladder."""
        reduction = {
            AutonomyLevel.FULL: AutonomyLevel.SUPERVISED,
            AutonomyLevel.SUPERVISED: AutonomyLevel.ADVISORY_ONLY,
            AutonomyLevel.ADVISORY_ONLY: AutonomyLevel.SUSPENDED,
            AutonomyLevel.SUSPENDED: AutonomyLevel.SUSPENDED,
        }
        return reduction[current]
