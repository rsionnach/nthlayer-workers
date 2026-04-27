"""Read-only assessment store backed by core API.

Used by the deploy gate CLI to read historical assessments from core.
Synchronous wrapper — safe for CLI use only (no running event loop).
Gate is CLI-only in v1.5; if gate moves to async (v2), use the
CoreAPIClient directly instead of this adapter.
"""

from __future__ import annotations

import asyncio

from nthlayer_common.api_client import CoreAPIClient

from nthlayer_workers.observe.assessment import Assessment, from_dict
from nthlayer_workers.observe.store import AssessmentFilter, AssessmentStore


class CoreAPIAssessmentStore(AssessmentStore):
    """Read-only assessment store backed by core API."""

    def __init__(self, client: CoreAPIClient):
        self._client = client

    def put(self, assessment: Assessment) -> None:
        raise NotImplementedError("CoreAPIAssessmentStore is read-only")

    def get(self, assessment_id: str) -> Assessment | None:
        # Not needed by gate; single-assessment lookup not exposed in v1.5.
        raise NotImplementedError("Single assessment lookup not needed by gate")

    def query(self, criteria: AssessmentFilter) -> list[Assessment]:
        result = asyncio.run(self._client.get_assessments(
            service=criteria.service,
            kind=criteria.kind,
            limit=criteria.limit if criteria.limit > 0 else 100,
        ))
        if not result.ok:
            return []
        return [from_dict(d) for d in result.data]
