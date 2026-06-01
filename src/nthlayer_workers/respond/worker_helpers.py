"""Helpers for opening IncidentContext from triggers.

``open_from_snapshot`` and ``open_from_breach`` build IncidentContext objects
from polled core API payloads. ``filter_after`` and ``breach_ids_with_snapshots``
support the cursor / dedup logic in ``RespondModule._ingest_*``. Topology tier
fields are populated by the worker via ``client.get_manifest()`` and passed
into the open helpers as the ``tiers`` mapping.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from nthlayer_workers.respond.config import RespondConfig
from nthlayer_workers.respond.types import (
    IncidentContext,
    IncidentState,
)


def _make_incident_id(service: str) -> str:
    """Build INC-<SERVICE>-YYYYMMDD-HHMMSS-<uuid8> id from current UTC time.

    Includes an 8-char uuid suffix to disambiguate incidents opened within
    the same second for the same service (rare but possible — two snapshots
    flushing back-to-back, or a snapshot + its orphan-breach fallback racing).
    """
    now = datetime.now(UTC)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    safe_svc = service.upper().replace("/", "-")
    suffix = uuid.uuid4().hex[:8]
    return f"INC-{safe_svc}-{stamp}-{suffix}"


def open_from_snapshot(
    snap: dict,
    config: RespondConfig,
    *,
    tiers: dict[str, str] | None = None,
) -> IncidentContext:
    """Build IncidentContext from a correlation_snapshot assessment.

    Primary trigger path — rich context from correlate's snapshot output.
    The optional ``tiers`` mapping (service -> tier) is supplied by the
    worker after fetching manifests from core; without it the triage agent
    would see a hardcoded ``"standard"`` tier for every service, which would
    distort severity derivation for critical services.
    """
    data = snap.get("data") or {}
    domain = data.get("domain") or {}
    service = domain.get("service") or "unknown"

    # parent_ids are the underlying breach verdict ids; trigger_ids prepends
    # the snapshot id but downstream case-creation anchors on the breach so
    # the bench's verdict-only ancestry walk resolves.
    # See docs/superpowers/decisions/verdict-assessment-taxonomy-boundary.md
    # See docs/superpowers/decisions/eager-case-creation.md
    parent_ids = list(data.get("parent_ids") or [])
    trigger_ids = [snap["id"], *parent_ids]
    now = datetime.now(UTC).isoformat()

    metadata: dict = {
        "blast_radius": data.get("blast_radius", []),
        "correlation_summary": data.get("nl_summary"),
        "peak_severity": data.get("peak_severity"),
        "event_count": data.get("event_count"),
        "affected_services": data.get("affected_services", []),
        # Capture-at-write-time: store threshold so escalation gate doesn't need
        # config back-reference to make decisions
        "escalation_threshold": config.escalation_threshold,
    }

    affected = data.get("affected_services") or [service]
    tiers = tiers or {}
    return IncidentContext(
        id=_make_incident_id(service),
        state=IncidentState.TRIGGERED,
        created_at=now,
        updated_at=now,
        trigger_source="nthlayer-correlate",
        trigger_verdict_ids=trigger_ids,
        topology={"services": [{"name": s, "tier": tiers.get(s, "standard")} for s in affected]},
        metadata=metadata,
    )


def open_from_breach(
    breach: dict,
    config: RespondConfig,
    *,
    tiers: dict[str, str] | None = None,
) -> IncidentContext:
    """Build IncidentContext from a quality_breach verdict (fallback path).

    Fallback trigger path — thinner context. The triage agent will produce
    a thinner initial briefing because there's no correlation snapshot to
    reference. Appropriate, since the fallback only fires when correlate is
    degraded.
    """
    service = breach.get("service") or breach.get("subject", {}).get("service") or "unknown"
    now = datetime.now(UTC).isoformat()

    metadata: dict = {
        # Static reason — threshold is logged separately by _ingest_orphan_breaches
        # so the metadata string stays stable if the threshold is configurable
        "fallback_reason": "no_correlation_snapshot",
        "severity": breach.get("severity") or breach.get("data", {}).get("severity"),
        "escalation_threshold": config.escalation_threshold,
    }

    tiers = tiers or {}
    return IncidentContext(
        id=_make_incident_id(service),
        state=IncidentState.TRIGGERED,
        created_at=now,
        updated_at=now,
        trigger_source="nthlayer-measure-fallback",
        trigger_verdict_ids=[breach["id"]],
        topology={"services": [{"name": service, "tier": tiers.get(service, "standard")}]},
        metadata=metadata,
    )


def filter_after(records: list, cursor: str | None) -> list:
    """Return records with created_at strictly greater than cursor."""
    if cursor is None:
        return list(records)
    return [r for r in records if (r.get("created_at") or "") > cursor]


def breach_ids_with_snapshots(snapshots: list[dict]) -> set[str]:
    """Set of breach IDs that already have an associated correlation snapshot.

    Used by the fallback path to dedup orphan breaches without an O(N) HTTP
    fan-out. v1.5 limitation: pagination of the snapshot list means breaches
    referenced only by snapshots beyond page 1 are not deduped — fallback
    will over-trigger an incident in that case (safer than dropping signals).
    """
    seen: set[str] = set()
    for snap in snapshots or []:
        data = snap.get("data") or {}
        for pid in (data.get("parent_ids") or []):
            if isinstance(pid, str):
                seen.add(pid)
    return seen
