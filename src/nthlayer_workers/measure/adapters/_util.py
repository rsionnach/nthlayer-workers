"""Shared utilities for polling adapters."""
from __future__ import annotations

from collections import OrderedDict

_MAX_SEEN = 10_000


class BoundedSeenSet:
    """A bounded set that evicts oldest entries when full."""

    def __init__(self, maxsize: int = _MAX_SEEN) -> None:
        self._data: OrderedDict[str, None] = OrderedDict()
        self._maxsize = maxsize

    def __contains__(self, item: str) -> bool:
        return item in self._data

    def add(self, item: str) -> None:
        if item in self._data:
            return
        if len(self._data) >= self._maxsize:
            self._data.popitem(last=False)
        self._data[item] = None
