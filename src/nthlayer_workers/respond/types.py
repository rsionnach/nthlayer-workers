# src/nthlayer_respond/types.py
"""Core data types for Mayday."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class IncidentState(str, Enum):
    TRIGGERED = "triggered"
    TRIAGING = "triaging"
    INVESTIGATING = "investigating"
    REMEDIATING = "remediating"
    AWAITING_APPROVAL = "awaiting_approval"
    RESOLVED = "resolved"
    ESCALATED = "escalated"
    FAILED = "failed"


TERMINAL_STATES = frozenset({
    IncidentState.RESOLVED,
    IncidentState.ESCALATED,
    IncidentState.FAILED,
})


class AgentRole(str, Enum):
    TRIAGE = "triage"
    INVESTIGATION = "investigation"
    COMMUNICATION = "communication"
    REMEDIATION = "remediation"


@dataclass
class TriageResult:
    severity: int  # 0-4 (P0-P4)
    blast_radius: list[str]
    affected_slos: list[str]
    assigned_team: str | None
    reasoning: str
    confidence: float | None = None  # from model response; None = not yet parsed


@dataclass
class Hypothesis:
    description: str
    confidence: float  # 0.0-1.0
    evidence: list[str]
    change_candidate: str | None


@dataclass
class InvestigationResult:
    hypotheses: list[Hypothesis]
    root_cause: str | None
    root_cause_confidence: float
    reasoning: str
    confidence: float | None = None


@dataclass
class CommunicationUpdate:
    channel: str
    timestamp: str  # ISO 8601
    update_type: str  # "initial" or "resolution"
    content: str


@dataclass
class CommunicationResult:
    updates_sent: list[CommunicationUpdate] = field(default_factory=list)
    reasoning: str = ""
    confidence: float | None = None


@dataclass
class RemediationResult:
    proposed_action: str | None = None
    target: str | None = None
    risk_assessment: str = ""
    requires_human_approval: bool = True
    executed: bool = False
    execution_result: str | None = None
    autonomy_reduced: bool = False
    autonomy_target: str | None = None
    previous_autonomy_level: str | None = None
    new_autonomy_level: str | None = None
    reasoning: str = ""
    autonomy_reduction: dict | None = None
    confidence: float | None = None


@dataclass
class IncidentContext:
    id: str  # INC-YYYY-NNNN
    state: IncidentState
    created_at: str  # ISO 8601
    updated_at: str
    trigger_source: str  # "nthlayer-correlate", "pagerduty", "manual"
    trigger_verdict_ids: list[str]
    topology: dict
    triage: TriageResult | None = None
    investigation: InvestigationResult | None = None
    communication: CommunicationResult | None = None
    remediation: RemediationResult | None = None
    verdict_chain: list[str] = field(default_factory=list)
    last_completed_step_index: int | None = None  # pipeline step 0-3
    error: str | None = None
    metadata: dict = field(default_factory=dict)
