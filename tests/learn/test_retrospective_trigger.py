"""CLI-path retrospective trigger_service wiring (opensrm-dpws)."""
from __future__ import annotations

from nthlayer_common.verdicts.core import create
from nthlayer_common.verdicts.store import MemoryStore
from nthlayer_workers.learn.retrospective import build_retrospective


def _make_incident(service: str | None):
    """Create an incident-like verdict with the given subject.service.

    Uses subject.type='custom' because nthlayer_common's VALID_SUBJECT_TYPES
    doesn't have a dedicated 'incident' bucket; 'custom' is the canonical
    catch-all and build_retrospective doesn't inspect the incident's own
    subject.type (only filters lineage verdicts by type).
    """
    return create(
        subject={
            "type": "custom",
            "ref": "INC-1",
            "service": service,
            "summary": "test incident",
        },
        judgment={"action": "flag", "confidence": 0.9, "reasoning": "test"},
        producer={"system": "test"},
        metadata={"custom": {"incident_id": "INC-1"}},
    )


def _make_correlation(service: str | None):
    return create(
        subject={
            "type": "correlation",
            "ref": "csn-1",
            "service": service,
            "summary": "correlation snapshot",
        },
        judgment={"action": "flag", "confidence": 0.7, "reasoning": "test"},
        producer={"system": "nthlayer-correlate"},
        metadata={"custom": {"root_causes": []}},
    )


class TestTriggerServiceResolution:
    """opensrm-dpws: build_retrospective populates metadata.custom['trigger_service']."""

    def test_trigger_service_from_correlation_verdict(self):
        """Correlation verdict's subject.service wins over incident's."""
        store = MemoryStore()
        incident = _make_incident("fallback-service")
        correlation = _make_correlation("fraud-detect")
        store.put(incident)
        store.put(correlation)
        # Link correlation as ancestor of incident
        incident.lineage.context = [correlation.id]
        store.put(incident)

        retro = build_retrospective(incident.id, store)
        assert retro.metadata.custom["trigger_service"] == "fraud-detect"

    def test_trigger_service_fallback_to_incident_subject(self):
        """No correlation in lineage → falls back to incident.subject.service."""
        store = MemoryStore()
        incident = _make_incident("payments")
        store.put(incident)

        retro = build_retrospective(incident.id, store)
        assert retro.metadata.custom["trigger_service"] == "payments"

    def test_trigger_service_omitted_when_neither(self):
        """Both correlation absent and incident.subject.service empty → key absent."""
        store = MemoryStore()
        incident = _make_incident(None)
        store.put(incident)

        retro = build_retrospective(incident.id, store)
        assert "trigger_service" not in retro.metadata.custom

    def test_trigger_service_skips_empty_correlation_subject_to_fallback(self):
        """Correlation present but subject.service is empty → fallback wins."""
        store = MemoryStore()
        incident = _make_incident("payments")
        correlation = _make_correlation(None)
        store.put(incident)
        store.put(correlation)
        incident.lineage.context = [correlation.id]
        store.put(incident)

        retro = build_retrospective(incident.id, store)
        assert retro.metadata.custom["trigger_service"] == "payments"

    def test_trigger_service_skips_whitespace_correlation_subject_to_fallback(self):
        """Correlation subject.service is whitespace-only → fallback wins.
        R5 edge-cases: whitespace strings are not valid service identities."""
        store = MemoryStore()
        incident = _make_incident("payments")
        correlation = _make_correlation("   ")
        store.put(incident)
        store.put(correlation)
        incident.lineage.context = [correlation.id]
        store.put(incident)

        retro = build_retrospective(incident.id, store)
        assert retro.metadata.custom["trigger_service"] == "payments"

    def test_trigger_service_whitespace_fallback_omits_key(self):
        """Both correlation and fallback are whitespace-only → key omitted."""
        store = MemoryStore()
        incident = _make_incident("   ")
        store.put(incident)

        retro = build_retrospective(incident.id, store)
        assert "trigger_service" not in retro.metadata.custom

    def test_trigger_service_stripped_when_padded(self):
        """Padded service name is stripped before write — no `' fraud-detect '` keys."""
        store = MemoryStore()
        incident = _make_incident("  fraud-detect  ")
        store.put(incident)

        retro = build_retrospective(incident.id, store)
        assert retro.metadata.custom["trigger_service"] == "fraud-detect"
