"""Alert suppression — 'don't page me for this'.

Suppression rules let SREs mute known non-issues (e.g., nightly backup
latency spikes) with a baseline + override threshold. If the current
value exceeds the override threshold, the suppression is overridden
and the SRE is paged with context explaining why.

No model call. Baseline is arithmetic (historical mean). Override
detection is a single comparison.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_DEFAULT_OVERRIDE_MULTIPLIER = 3.0
_DEFAULT_REVIEW_DAYS = 30


@dataclass
class Suppression:
    """A suppression rule for a service metric."""

    service: str
    metric: str
    window: dict[str, Any]  # {type, start, end, timezone?}
    reason: str
    baseline: float
    override_threshold: float
    created_by: str = "unknown"
    created_at: datetime | None = None
    review_after: datetime | None = None


def create_suppression(
    *,
    service: str,
    metric: str,
    window: dict[str, Any],
    reason: str,
    baseline: float,
    override_multiplier: float = _DEFAULT_OVERRIDE_MULTIPLIER,
    created_by: str = "unknown",
) -> Suppression:
    """Create a suppression rule from a baseline measurement.

    The override threshold is ``baseline * override_multiplier``.
    If the metric exceeds this during the suppressed window,
    the suppression is overridden and the SRE is paged.
    """
    if baseline <= 0:
        msg = f"Baseline must be positive, got {baseline}"
        raise ValueError(msg)

    now = datetime.now(timezone.utc)
    override_threshold = baseline * override_multiplier

    logger.info(
        "suppression_created",
        service=service,
        metric=metric,
        baseline=baseline,
        override_threshold=override_threshold,
        reason=reason,
    )

    return Suppression(
        service=service,
        metric=metric,
        window=window,
        reason=reason,
        baseline=baseline,
        override_threshold=override_threshold,
        created_by=created_by,
        created_at=now,
        review_after=now + timedelta(days=_DEFAULT_REVIEW_DAYS),
    )


def check_suppression_override(suppression: Suppression, current_value: float) -> bool:
    """Return True if the current value exceeds the override threshold.

    At exactly the threshold, suppression holds (must strictly exceed).
    """
    return current_value > suppression.override_threshold
