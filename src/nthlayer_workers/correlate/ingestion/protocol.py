"""Ingester protocol — the ingestion contract for SitRep."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from nthlayer_workers.correlate.types import SitRepEvent


class Ingester(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def on_event(self, handler: Callable[[SitRepEvent], Awaitable[None] | None]) -> None: ...
