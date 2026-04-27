"""Core data types for SitRep."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventType(str, Enum):
    ALERT = "alert"
    METRIC_BREACH = "metric_breach"
    CHANGE = "change"
    QUALITY_SCORE = "quality_score"
    VERDICT = "verdict"
    CUSTOM = "custom"


@dataclass
class SitRepEvent:
    id: str
    timestamp: str  # ISO 8601
    source: str
    type: EventType
    service: str
    environment: str
    severity: float  # 0.0-1.0
    payload: dict[str, Any]
    dependencies: list[str] = field(default_factory=list)
    dependents: list[str] = field(default_factory=list)
    ttl: int = 86400  # seconds, default 24h


@dataclass
class TemporalGroup:
    service: str
    time_window: tuple[str, str]  # (start, end) ISO 8601
    events: list[SitRepEvent]
    count: int
    peak_severity: float
    duration_seconds: float


@dataclass
class ChangeCandidate:
    change: SitRepEvent
    affected_service: str
    temporal_proximity_seconds: float
    same_service: bool
    dependency_related: bool


@dataclass
class TopologyCorrelation:
    primary_service: str
    related_services: list[dict[str, Any]]  # [{service, relationship, events}]
    topology_path: list[str]


@dataclass
class CorrelationGroup:
    id: str
    priority: int  # 0=P0 (critical), 1=P1, 2=P2, 3=P3
    summary: str  # template-generated, NOT model
    services: list[str]
    signals: list[TemporalGroup]
    topology: TopologyCorrelation | None
    change_candidates: list[ChangeCandidate]
    first_seen: str
    last_updated: str
    event_count: int


class AgentState(str, Enum):
    WATCHING = "watching"
    ALERT = "alert"
    INCIDENT = "incident"
    DEGRADED = "degraded"
