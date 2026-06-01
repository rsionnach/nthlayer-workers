"""Agent state machine for SitRep."""
from __future__ import annotations

from datetime import UTC, datetime

from nthlayer_workers.correlate.types import AgentState, CorrelationGroup


class StateMachine:
    """Deterministic state machine. Transitions are transport, not judgment."""

    def __init__(self):
        self.state = AgentState.WATCHING
        self._alert_start: datetime | None = None
        self._calm_since: datetime | None = None

    def update(
        self,
        groups: list[CorrelationGroup],
        model_healthy: bool = True,
    ) -> AgentState:
        """Evaluate state transition based on correlation output."""
        now = datetime.now(UTC)

        # Model failure → DEGRADED (from any state except INCIDENT)
        if not model_healthy and self.state != AgentState.INCIDENT:
            self.state = AgentState.DEGRADED
            self._calm_since = None
            return self.state

        # DEGRADED + model recovery → WATCHING
        if self.state == AgentState.DEGRADED and model_healthy:
            self.state = AgentState.WATCHING
            self._calm_since = now
            return self.state

        has_p0 = any(g.priority == 0 for g in groups)
        p1_count = sum(1 for g in groups if g.priority == 1)
        has_elevated = has_p0 or p1_count >= 2

        if self.state == AgentState.WATCHING:
            if has_elevated:
                self.state = AgentState.ALERT
                self._alert_start = now
                self._calm_since = None
        elif self.state == AgentState.ALERT:
            if has_elevated:
                self._calm_since = None
            else:
                if self._calm_since is None:
                    self._calm_since = now
                elif (now - self._calm_since).total_seconds() >= 600:  # 10 min
                    self.state = AgentState.WATCHING
                    self._alert_start = None
                    self._calm_since = None

        return self.state

    def declare_incident(self) -> None:
        """External incident declaration → INCIDENT."""
        self.state = AgentState.INCIDENT
        self._calm_since = None

    def resolve_incident(self) -> None:
        """Incident resolved → WATCHING."""
        self.state = AgentState.WATCHING
        self._alert_start = None
        self._calm_since = datetime.now(UTC)

    def get_interval(self) -> int:
        """Snapshot interval in seconds for current state."""
        intervals = {
            AgentState.WATCHING: 300,
            AgentState.ALERT: 60,
            AgentState.INCIDENT: 30,
            AgentState.DEGRADED: 120,
        }
        return intervals[self.state]

    def get_cache_ttl(self) -> int | None:
        """Cache TTL in seconds. None = no caching (INCIDENT)."""
        ttls = {
            AgentState.WATCHING: 900,  # 15 min
            AgentState.ALERT: 300,     # 5 min
            AgentState.INCIDENT: None, # no caching
            AgentState.DEGRADED: 600,  # 10 min
        }
        return ttls[self.state]
