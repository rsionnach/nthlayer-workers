"""Detection protocol — pure interface for degradation detection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from nthlayer_workers.measure.types import TrendWindow


@dataclass(frozen=True)
class Alert:
    """A degradation alert raised when a metric crosses a threshold."""

    agent_name: str
    metric_name: str
    current_value: float
    threshold: float
    message: str


class DegradationDetector(Protocol):
    """Compares TrendWindow values against human-declared thresholds.

    Pure arithmetic comparison — code doesn't decide what's "bad",
    config declares the thresholds (ZFC).
    """

    def check(self, window: TrendWindow) -> list[Alert]: ...
