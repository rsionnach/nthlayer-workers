"""Verdict-based calibration — system-wide accuracy via verdict store.

Strangler fig: runs alongside JudgmentSLOChecker, does not replace it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from nthlayer_common.verdicts import AccuracyFilter, AccuracyReport, VerdictStore


class VerdictCalibration:
    """System-wide accuracy for all arbiter verdicts.

    Note: this is NOT per-agent. AccuracyFilter does not support
    filtering by subject.agent. Per-agent accuracy would require
    extending the verdict library's AccuracyFilter (Phase 2+).
    For Phase 1, system-wide accuracy is sufficient for Demo 1.
    """

    def __init__(self, verdict_store: VerdictStore) -> None:
        self._store = verdict_store

    async def check(self, window_days: int = 30) -> AccuracyReport:
        from_time = datetime.now(UTC) - timedelta(days=window_days)
        return await asyncio.to_thread(
            self._store.accuracy,
            AccuracyFilter(producer_system="arbiter", from_time=from_time),
        )
