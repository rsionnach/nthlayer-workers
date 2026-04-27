"""Incident envelope creation for the decision records system.

Observe creates incident envelopes when a breach assessment triggers a respond cycle.
The incident_id is assigned at trigger time and propagated to all downstream records.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from nthlayer_common.records.models import Incident
from nthlayer_common.records.sqlite_store import SQLiteDecisionRecordStore

__all__ = ["create_incident_from_breach"]


def create_incident_from_breach(
    store: SQLiteDecisionRecordStore,
    trigger_hash: str,
    stream: str,
) -> Incident:
    """Create an incident envelope from a breach assessment.

    The incident is written to the store and returned. The caller should use
    the ``incident.id`` to stamp downstream records (verdicts, evaluations).
    """
    incident_id = f"inc-{uuid.uuid4().hex[:12]}"
    incident = Incident(
        id=incident_id,
        created_at=datetime.now(timezone.utc),
        trigger_hash=trigger_hash,
        stream=stream,
    )
    store.create_incident(incident)
    return incident
