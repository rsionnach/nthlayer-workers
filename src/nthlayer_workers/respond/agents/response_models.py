"""Pydantic response models for Instructor-backed agent calls (P3-E.2).

One model per agent. Instructor enforces schema with retry-on-validation
(``max_retries`` in ``structured_call_with_usage``). The agent's
``parse_response`` continues to accept a JSON string — the Pydantic
instance is dumped back to JSON via ``model_dump_json()`` to reuse the
existing dict→dataclass conversion logic without rewriting every test
that mocks a raw JSON response.

Field names match the canonical shape the existing parse_response
expects. Aliases for legacy variants (``team_assignment``,
``hypothesis``, ``rationale``, etc.) live in parse_response, not here:
the canonical path goes through these clean names.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class TriageResponse(BaseModel):
    """TriageAgent's structured output."""

    severity: int = Field(ge=0, le=4, description="Incident priority 0 (P0) to 4 (P4)")
    blast_radius: list[str] = Field(default_factory=list, description="Affected service names")
    affected_slos: list[str] = Field(default_factory=list, description="SLO names breached or at risk")
    assigned_team: str | None = Field(default=None, description="Team owning the response")
    reasoning: str = Field(default="", description="Brief explanation of the triage decision")
    confidence: float = Field(ge=0.0, le=1.0, default=0.0, description="Model confidence in the triage call")


class HypothesisModel(BaseModel):
    """One hypothesis in InvestigationResponse."""

    description: str = Field(description="Hypothesis statement")
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    evidence: list[str] = Field(default_factory=list, description="Supporting evidence references")
    change_candidate: str | None = Field(default=None, description="Suspect change/deploy id, if any")


class InvestigationResponse(BaseModel):
    """InvestigationAgent's structured output."""

    hypotheses: list[HypothesisModel] = Field(default_factory=list)
    root_cause: str | None = Field(default=None, description="Declared root cause; null if confidence below threshold")
    root_cause_confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    reasoning: str = Field(default="")
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)


class CommunicationUpdateModel(BaseModel):
    """One communication update in CommunicationResponse."""

    channel: str = Field(default="status_page")
    update_type: str = Field(default="initial", description="initial | resolution")
    content: str = Field(default="")


class CommunicationResponse(BaseModel):
    """CommunicationAgent's structured output."""

    updates: list[CommunicationUpdateModel] = Field(default_factory=list)
    reasoning: str = Field(default="")
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)


class AutonomyReductionRequest(BaseModel):
    """Optional autonomy reduction nested under RemediationResponse."""

    recommended: bool = Field(default=False)
    target_agent: str = Field(default="")
    reason: str = Field(default="")


class RemediationResponse(BaseModel):
    """RemediationAgent's structured output."""

    proposed_action: str | None = Field(default=None, description="Action name from the safe-action registry")
    target: str | None = Field(default=None, description="Service or resource the action targets")
    risk_assessment: str = Field(default="")
    requires_human_approval: bool = Field(default=True, description="Approval gate; only escalation honored — never downgraded by model")
    autonomy_reduction: AutonomyReductionRequest = Field(default_factory=AutonomyReductionRequest)
    reasoning: str = Field(default="")
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
