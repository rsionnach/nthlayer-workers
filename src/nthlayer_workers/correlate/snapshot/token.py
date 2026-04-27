"""Token estimation. Protocol + simple char/4 estimator for Tier 1."""
from __future__ import annotations

from typing import Protocol


class TokenEstimator(Protocol):
    def estimate(self, text: str) -> int: ...


class CharDivFourEstimator:
    """len(text) // 4 — good enough for Tier 1."""
    def estimate(self, text: str) -> int:
        return len(text) // 4
