# tests/conftest.py
"""Shared test fixtures for Mayday."""
from __future__ import annotations

import pytest
from nthlayer_common.verdicts import MemoryStore

from nthlayer_workers.respond.types import IncidentContext, IncidentState


@pytest.fixture
def verdict_store():
    return MemoryStore()


@pytest.fixture
def sample_context():
    return IncidentContext(
        id="INC-2026-0001",
        state=IncidentState.TRIGGERED,
        created_at="2026-03-19T10:00:00Z",
        updated_at="2026-03-19T10:00:00Z",
        trigger_source="nthlayer-correlate",
        trigger_verdict_ids=["vrd-2026-03-19-abc12345-00001"],
        topology={
            "services": [
                {"name": "payment-api", "tier": "critical", "dependencies": ["database-primary"]},
                {"name": "checkout-service", "tier": "critical", "dependencies": ["payment-api"]},
            ]
        },
    )


@pytest.fixture
def sample_context_pagerduty():
    return IncidentContext(
        id="INC-2026-0002",
        state=IncidentState.TRIGGERED,
        created_at="2026-03-19T10:00:00Z",
        updated_at="2026-03-19T10:00:00Z",
        trigger_source="pagerduty",
        trigger_verdict_ids=[],
        topology={},
    )
