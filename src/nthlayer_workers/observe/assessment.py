"""Assessment dataclass — a deterministic observation of system state."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from nthlayer_common.cloudevents import ASSESSMENT_KINDS

# Canonical kinds from common + observe-internal types not in CloudEvents
# taxonomy (verification is CLI-only, never submitted to core).
VALID_ASSESSMENT_TYPES = ASSESSMENT_KINDS | frozenset({"verification"})

_id_lock = threading.Lock()
_id_sequence = 0


def _generate_id() -> str:
    """Generate a unique assessment ID. Thread-safe."""
    global _id_sequence
    with _id_lock:
        _id_sequence += 1
        seq = _id_sequence
    short_uuid = uuid.uuid4().hex[:8]
    now = datetime.now(timezone.utc)
    date_part = now.strftime("%Y-%m-%d")
    return f"asm-{date_part}-{short_uuid}-{seq:05d}"


@dataclass
class Assessment:
    """A deterministic observation of system state.

    NOT a verdict. No judgment, no confidence score, no LLM reasoning.
    Same inputs always produce the same assessment.
    """

    id: str
    created_at: datetime
    kind: str
    service: str
    producer: str
    data: dict


def create(
    kind: str,
    service: str,
    data: dict,
    *,
    producer: str = "nthlayer-observe",
) -> Assessment:
    """Create a new Assessment with generated ID and UTC timestamp."""
    if kind not in VALID_ASSESSMENT_TYPES:
        raise ValueError(
            f"Invalid kind {kind!r}, "
            f"must be one of {sorted(VALID_ASSESSMENT_TYPES)}"
        )
    return Assessment(
        id=_generate_id(),
        created_at=datetime.now(timezone.utc),
        kind=kind,
        service=service,
        producer=producer,
        data=data,
    )


def to_dict(assessment: Assessment) -> dict:
    """Serialize an Assessment to a plain dict."""
    return {
        "id": assessment.id,
        "created_at": assessment.created_at.isoformat(),
        "kind": assessment.kind,
        "service": assessment.service,
        "producer": assessment.producer,
        "data": assessment.data,
    }


def from_dict(raw: dict) -> Assessment:
    """Deserialize an Assessment from a plain dict."""
    ts = raw["created_at"]
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    return Assessment(
        id=raw["id"],
        created_at=ts,
        kind=raw["kind"],
        service=raw["service"],
        producer=raw["producer"],
        data=raw["data"],
    )
