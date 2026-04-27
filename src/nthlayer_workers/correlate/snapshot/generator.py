"""Snapshot generator with token budget, priority tiers, and caching."""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass

from nthlayer_workers.correlate.snapshot.token import CharDivFourEstimator, TokenEstimator
from nthlayer_workers.correlate.types import AgentState, CorrelationGroup


@dataclass
class SnapshotBudget:
    max_tokens: int = 4000
    reserved_instructions: int = 500
    reserved_history: int = 500

    @property
    def available_for_groups(self) -> int:
        return self.max_tokens - self.reserved_instructions - self.reserved_history


class SnapshotGenerator:
    def __init__(
        self,
        budget: SnapshotBudget | None = None,
        token_estimator: TokenEstimator | None = None,
        cache_ttl_seconds: int = 900,  # 15 min default
    ):
        self._budget = budget or SnapshotBudget()
        self._estimator = token_estimator or CharDivFourEstimator()
        self._cache_ttl = cache_ttl_seconds
        self._cached_hash: str | None = None
        self._cached_prompt: str | None = None
        self._cache_time: float = 0.0

    def generate(
        self,
        groups: list[CorrelationGroup],
        state: AgentState = AgentState.WATCHING,
        service_context: dict | None = None,
        force_refresh: bool = False,
    ) -> tuple[str, bool]:
        """Generate model prompt from correlation groups.

        Returns (model_prompt, cache_hit).
        If cache_hit is True, model_prompt is the cached prompt.
        """
        # Compute content hash
        content_hash = self._compute_hash(groups)

        # Check cache (skip in INCIDENT mode)
        if not force_refresh and state != AgentState.INCIDENT:
            if self._is_cache_valid(content_hash, state):
                return self._cached_prompt or "", True

        # Sort groups by priority (P0 first)
        sorted_groups = sorted(groups, key=lambda g: g.priority)

        # Apply token budget
        included_groups = self._apply_budget(sorted_groups)

        # Assemble prompt
        prompt = self._assemble_prompt(included_groups, service_context)

        # Update cache
        self._cached_hash = content_hash
        self._cached_prompt = prompt
        self._cache_time = time.monotonic()

        return prompt, False

    def _compute_hash(self, groups: list[CorrelationGroup]) -> str:
        """SHA256 of stable group content (services, event counts, priority)."""
        parts = sorted(
            f"{','.join(sorted(g.services))}:{g.event_count}:{g.priority}"
            for g in groups
        )
        return hashlib.sha256("|".join(parts).encode()).hexdigest()

    def _is_cache_valid(self, content_hash: str, state: AgentState) -> bool:
        """Check if cached snapshot is still valid."""
        if self._cached_hash is None:
            return False
        if content_hash != self._cached_hash:
            return False
        # Check TTL
        elapsed = time.monotonic() - self._cache_time
        ttl = {
            AgentState.WATCHING: 900,
            AgentState.ALERT: 300,
            AgentState.INCIDENT: 0,  # never cache
            AgentState.DEGRADED: 600,
        }.get(state, self._cache_ttl)
        return elapsed < ttl

    def _apply_budget(self, sorted_groups: list[CorrelationGroup]) -> list[CorrelationGroup]:
        """Include groups until token budget exhausted. P0 always included."""
        included = []
        tokens_used = 0
        budget = self._budget.available_for_groups

        for group in sorted_groups:
            group_text = self._serialize_group(group)
            group_tokens = self._estimator.estimate(group_text)

            if group.priority == 0:
                # P0 always included, even over budget
                included.append(group)
                tokens_used += group_tokens
            elif tokens_used + group_tokens <= budget:
                included.append(group)
                tokens_used += group_tokens
            # else: drop (never truncate)

        return included

    def _serialize_group(self, group: CorrelationGroup) -> str:
        """Serialize a group to text for token counting and prompt inclusion."""
        lines = [
            f"## Correlation Group: {group.summary}",
            f"Priority: P{group.priority}",
            f"Services: {', '.join(group.services)}",
            f"Events: {group.event_count}",
            f"First seen: {group.first_seen}",
        ]

        if group.change_candidates:
            lines.append("Change candidates:")
            for cc in group.change_candidates:
                proximity_min = cc.temporal_proximity_seconds / 60
                relation = "same service" if cc.same_service else "dependency"
                lines.append(
                    f"  - {cc.change.payload.get('change_type', 'unknown')} on "
                    f"{cc.affected_service} ({proximity_min:.0f}m ago, {relation})"
                )

        if group.topology:
            lines.append(f"Topology: {' -> '.join(group.topology.topology_path)}")

        return "\n".join(lines)

    def _assemble_prompt(
        self,
        groups: list[CorrelationGroup],
        service_context: dict | None,
    ) -> str:
        """Assemble the full model prompt."""
        parts = []

        if not groups:
            parts.append("No elevated correlation groups detected. System appears healthy.")
            return "\n\n".join(parts)

        parts.append(f"Analyzing {len(groups)} correlation group(s):\n")

        for group in groups:
            parts.append(self._serialize_group(group))

        if service_context:
            parts.append("\nService context:")
            parts.append(json.dumps(service_context, indent=2))

        return "\n\n".join(parts)

    def invalidate_cache(self) -> None:
        """Manually invalidate the cache (e.g., on state transition)."""
        self._cached_hash = None
        self._cached_prompt = None
