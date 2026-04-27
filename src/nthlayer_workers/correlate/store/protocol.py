"""EventStore protocol — the storage contract for SitRep."""
from __future__ import annotations

from typing import Any, Protocol

from nthlayer_workers.correlate.types import EventType, SitRepEvent


class EventStore(Protocol):
    """Protocol defining the storage interface for SitRep events."""

    def insert(self, event: SitRepEvent) -> None: ...

    def insert_batch(self, events: list[SitRepEvent]) -> None: ...

    def get_by_time_window(
        self,
        start: str,
        end: str,
        *,
        service: str | None = None,
        event_type: EventType | None = None,
        min_severity: float | None = None,
    ) -> list[SitRepEvent]: ...

    def search(
        self,
        query: str,
        *,
        limit: int = 100,
        time_window: tuple[str, str] | None = None,
        service: str | None = None,
    ) -> list[SitRepEvent]: ...

    def get_by_topology(
        self, service: str, hops: int = 1
    ) -> list[SitRepEvent]: ...

    def get_recent_changes(
        self, service: str, window_minutes: int = 30,
        reference_time: str | None = None,
    ) -> list[SitRepEvent]: ...

    def expire_old(self) -> int: ...

    def get_state_hash(self, time_window: tuple[str, str]) -> str: ...

    def get_stats(self) -> dict[str, Any]: ...
