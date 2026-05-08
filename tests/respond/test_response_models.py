"""Tests for the agent Pydantic response models + stub coverage (P3-E.2)."""
from __future__ import annotations

import pytest

from nthlayer_workers.respond.agents.response_models import (
    CommunicationResponse,
    InvestigationResponse,
    RemediationResponse,
    TriageResponse,
)


class TestResponseModels:
    """Smoke-tests for the canonical model shapes."""

    def test_triage_severity_bounds(self):
        with pytest.raises(Exception):
            TriageResponse(severity=5)  # > 4 invalid
        with pytest.raises(Exception):
            TriageResponse(severity=-1)
        TriageResponse(severity=0)
        TriageResponse(severity=4)

    def test_triage_confidence_bounds(self):
        with pytest.raises(Exception):
            TriageResponse(severity=2, confidence=1.5)
        with pytest.raises(Exception):
            TriageResponse(severity=2, confidence=-0.1)
        TriageResponse(severity=2, confidence=0.5)

    def test_remediation_default_requires_approval(self):
        # Default is True — model must explicitly opt out, which the
        # registry-side approval ratchet then re-overrides anyway.
        r = RemediationResponse()
        assert r.requires_human_approval is True

    def test_investigation_minimal(self):
        r = InvestigationResponse()
        assert r.hypotheses == []
        assert r.root_cause is None

    def test_communication_minimal(self):
        r = CommunicationResponse()
        assert r.updates == []


class TestLLMStubFactories:
    """Canned-LLM stub returns valid instances of each agent response model.

    Exercised by integration-three-tier.sh (NTHLAYER_LLM_STUB=canned).
    A new agent type must register a factory here; this test catches the
    "I added an agent and forgot the stub" failure mode.
    """

    def test_triage_factory_registered(self, monkeypatch):
        from nthlayer_common.llm_structured import structured_call_with_usage
        monkeypatch.setenv("NTHLAYER_LLM_STUB", "canned")
        result = structured_call_with_usage("s", "u", TriageResponse)
        assert isinstance(result.data, TriageResponse)
        assert 0 <= result.data.severity <= 4

    def test_investigation_factory_registered(self, monkeypatch):
        from nthlayer_common.llm_structured import structured_call_with_usage
        monkeypatch.setenv("NTHLAYER_LLM_STUB", "canned")
        result = structured_call_with_usage("s", "u", InvestigationResponse)
        assert isinstance(result.data, InvestigationResponse)
        assert len(result.data.hypotheses) >= 1

    def test_communication_factory_registered(self, monkeypatch):
        from nthlayer_common.llm_structured import structured_call_with_usage
        monkeypatch.setenv("NTHLAYER_LLM_STUB", "canned")
        result = structured_call_with_usage("s", "u", CommunicationResponse)
        assert isinstance(result.data, CommunicationResponse)
        assert len(result.data.updates) >= 1

    def test_remediation_factory_registered(self, monkeypatch):
        from nthlayer_common.llm_structured import structured_call_with_usage
        monkeypatch.setenv("NTHLAYER_LLM_STUB", "canned")
        result = structured_call_with_usage("s", "u", RemediationResponse)
        assert isinstance(result.data, RemediationResponse)
        # The canned response uses a real safe-action name so the
        # registry's KeyError path is not falsely triggered.
        assert result.data.proposed_action == "rollback"
        assert result.data.requires_human_approval is True
