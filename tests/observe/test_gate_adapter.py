"""Tests for CoreAPIAssessmentStore — read-only assessment store backed by core API."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nthlayer_common.api_client import APIResult
from nthlayer_workers.observe.gate_adapter import CoreAPIAssessmentStore
from nthlayer_workers.observe.store import AssessmentFilter


class TestCoreAPIAssessmentStore:
    def test_put_raises(self):
        store = CoreAPIAssessmentStore(client=AsyncMock())
        with pytest.raises(NotImplementedError, match="read-only"):
            store.put(None)

    def test_get_raises(self):
        store = CoreAPIAssessmentStore(client=AsyncMock())
        with pytest.raises(NotImplementedError, match="not needed"):
            store.get("asm-001")

    def test_query_returns_assessments_on_success(self):
        client = AsyncMock()
        client.get_assessments = AsyncMock(return_value=APIResult(
            ok=True, status_code=200, data=[
                {
                    "id": "asm-001",
                    "created_at": "2026-04-24T00:00:00+00:00",
                    "kind": "slo_status",
                    "service": "payment-api",
                    "producer": "nthlayer-observe",
                    "data": {"status": "HEALTHY"},
                }
            ],
        ))

        store = CoreAPIAssessmentStore(client=client)
        results = store.query(AssessmentFilter(service="payment-api", kind="slo_status"))
        assert len(results) == 1
        assert results[0].id == "asm-001"
        assert results[0].kind == "slo_status"

    def test_query_returns_empty_on_api_failure(self):
        client = AsyncMock()
        client.get_assessments = AsyncMock(return_value=APIResult(
            ok=False, status_code=503, error="unavailable",
        ))

        store = CoreAPIAssessmentStore(client=client)
        results = store.query(AssessmentFilter(service="svc"))
        assert results == []
