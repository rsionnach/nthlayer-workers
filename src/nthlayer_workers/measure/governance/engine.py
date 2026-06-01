"""Governance engine Protocol.

The legacy ErrorBudgetGovernance concrete impl (LLM-driven autonomy
ladder) was retired under opensrm-t5yr — superseded by the
deterministic severity-based governance in measure/worker.py
(P3-C.2). The Protocol stays because pipeline/router.py still
accepts an optional `governance: GovernanceEngine | None` parameter
for future deterministic or model-driven implementations.

Key safety constraint preserved by any future impl: can REDUCE
autonomy without approval, never INCREASE without a human approver
(one-way ratchet — see `restore_autonomy`).
"""

from __future__ import annotations

from typing import Protocol

from nthlayer_workers.measure.types import AutonomyLevel, GovernanceAction


class GovernanceEngine(Protocol):
    """Manages agent autonomy levels based on evaluation trends."""

    async def check_agent(self, agent_name: str) -> GovernanceAction | None: ...

    async def get_autonomy(self, agent_name: str) -> AutonomyLevel: ...

    async def restore_autonomy(self, agent_name: str, level: AutonomyLevel, approver: str) -> None:
        """Restore autonomy — requires human approver (safety ratchet)."""
        ...
