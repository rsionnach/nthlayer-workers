"""Identity and breach-transition state for judgment thresholds (opensrm-g32d)."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Any

from nthlayer_common.manifest import JudgmentThreshold

# After a breach, a key re-arms only when BOTH hold: it has been within for
# rearm_seconds (MeasureModule passes REARM_INTERVALS * threshold_interval_seconds)
# AND it was evaluated within at least REARM_INTERVALS times in a row (the CLI
# path's 3-window hysteresis). The count keeps hysteresis when evaluations
# are close together; the duration keeps it when they are far apart.
# Tuning belongs to ratchet configuration.
REARM_INTERVALS = 3


def threshold_key(service: str, t: JudgmentThreshold) -> str:
    """service:type:hash of what identifies the entry: its measurement fields,
    the metric's parameters, and the NAMES of its bounds. Bound VALUES are
    excluded, so tightening a bound keeps the key; segment order is irrelevant.
    Two distinct entries of one service can still share it (same names,
    different values): the phase then suffixes each with bounds_digest."""
    m = t.measurement
    signal = m.outcome_signal
    ident = {
        "bounds": sorted(t.bounds),
        "metric": m.metric,
        "source": m.source,
        "confidence_threshold": m.confidence_threshold,
        "outcome_signal": [signal.source, signal.window] if signal else None,
        "segment_dimension": m.segment_dimension,
        "segment_values": sorted(m.segment_values),
        "probe": [m.probe.frozen_input_set, m.probe.frequency] if m.probe else None,
        "window": m.window,
        "bins": m.bins,
    }
    digest = hashlib.sha256(json.dumps(ident, sort_keys=True).encode()).hexdigest()[:12]
    return f"{service}:{t.type}:{digest}"


def bounds_digest(t: JudgmentThreshold) -> str:
    """8 hex chars of sha256 over the sorted (name, value) bound items: what
    tells apart entries that share a threshold_key. Depends on the entry
    alone, never on its position, so reordering cannot move state."""
    items = sorted(t.bounds.items(), key=lambda kv: kv[0])
    return hashlib.sha256(json.dumps(items).encode()).hexdigest()[:8]


def as_timestamp(v: Any) -> float | None:
    """A finite int/float (not bool), else None."""
    if type(v) in (int, float) and math.isfinite(v):
        return float(v)
    return None


@dataclass
class KeyState:
    last_evaluable: str | None = None  # last "within" or "breach"; not_evaluable never sets it
    armed: bool = True
    within_since: float | None = None  # when the current within run began; None outside one
    within_count: int = 0  # within evaluations in the current run; not_evaluable leaves it

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Any) -> KeyState:
        """Read a persisted state. Restored component_state is not trusted: a
        non-dict, or a field of the wrong type, takes the default (armed, no
        within run), so a corrupt entry can re-raise a breach but never
        silences one. State written before within_since existed (it carried
        within_streak) reads as no within run."""
        if not isinstance(d, dict):
            return cls()
        last, armed, count = d.get("last_evaluable"), d.get("armed"), d.get("within_count")
        return cls(
            last_evaluable=last if last in ("within", "breach") else None,
            armed=armed if isinstance(armed, bool) else True,
            within_since=as_timestamp(d.get("within_since")),
            within_count=count if type(count) is int and count >= 0 else 0,
        )


def advance(state: KeyState, status: str, now: float, rearm_seconds: float) -> tuple[KeyState, bool]:
    """Apply one evaluation's combined status at time *now*. Returns (new
    state, fire a breach?).

    A disarmed key re-arms when its current within run is at least
    *rearm_seconds* old AND holds at least REARM_INTERVALS within
    evaluations. not_evaluable neither starts, extends nor ends a run; a
    breach ends it. A run that starts in the future (the clock went back)
    restarts now, so a backward clock delays re-arming but cannot stop it."""
    if status == "not_evaluable":
        return state, False
    if status == "within":
        restart = state.within_since is None or state.within_since > now
        since = now if restart else state.within_since
        count = 1 if restart else state.within_count + 1
        armed = state.armed or (now - since >= rearm_seconds and count >= REARM_INTERVALS)
        return KeyState("within", armed, since, count), False
    # breach. last_evaluable also guards a restored armed+breach state.
    fire = state.armed and state.last_evaluable != "breach"
    return KeyState("breach", state.armed and not fire, None, 0), fire
