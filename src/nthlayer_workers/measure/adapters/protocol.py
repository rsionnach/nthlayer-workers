"""Adapter protocol — the boundary between external systems and nthlayer-measure."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from nthlayer_workers.measure.types import AgentOutput


class Adapter(Protocol):
    """Translates external agent output formats into AgentOutput.

    Adapters are pure transport — they normalize data shape,
    never interpret quality.
    """

    def name(self) -> str: ...

    def receive(self) -> AsyncIterator[AgentOutput]: ...
