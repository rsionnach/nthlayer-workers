"""ScoreStore protocol — persistence boundary for evaluation results."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from nthlayer_workers.measure.types import QualityScore


class ScoreStore(Protocol):
    """Persists and retrieves evaluation scores.

    The store is pure transport — it saves and loads data,
    never interprets or transforms scores.
    """

    async def save_score(self, score: QualityScore) -> None: ...

    async def get_scores(
        self, agent_name: str, since: datetime, limit: int = 100
    ) -> list[QualityScore]: ...

    async def save_override(
        self, eval_id: str, corrected_dimensions: dict[str, float], corrector: str
    ) -> None: ...

    async def get_overrides(
        self, since: datetime, limit: int = 100, agent_name: str | None = None
    ) -> list[dict]: ...

    async def get_autonomy(self, agent_name: str) -> str | None: ...

    async def set_verdict_id(self, eval_id: str, verdict_id: str) -> None: ...

    async def set_autonomy(
        self, agent_name: str, level: str, updated_by: str
    ) -> None: ...
