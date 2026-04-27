"""Assessment store interface and in-memory implementation."""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from nthlayer_workers.observe.assessment import Assessment


@dataclass
class AssessmentFilter:
    """Filter criteria for querying assessments."""

    service: str | None = None
    kind: str | None = None
    producer: str | None = None
    from_time: datetime | None = None
    to_time: datetime | None = None
    limit: int = 100  # 0 = unlimited


class AssessmentStore(ABC):
    """Abstract interface for assessment storage."""

    @abstractmethod
    def put(self, assessment: Assessment) -> None:
        """Store an assessment. Raises ValueError if ID already exists."""

    @abstractmethod
    def get(self, assessment_id: str) -> Assessment | None:
        """Retrieve an assessment by ID. Returns None if not found."""

    @abstractmethod
    def query(self, criteria: AssessmentFilter) -> list[Assessment]:
        """Query assessments matching filter, ordered by created_at descending."""

    def get_latest(self, service: str, kind: str) -> Assessment | None:
        """Return the most recent assessment for a service+kind combination."""
        results = self.query(
            AssessmentFilter(service=service, kind=kind, limit=1)
        )
        return results[0] if results else None


class MemoryAssessmentStore(AssessmentStore):
    """Thread-safe in-memory assessment store for tests."""

    def __init__(self) -> None:
        self._assessments: dict[str, Assessment] = {}
        self._lock = threading.Lock()

    def put(self, assessment: Assessment) -> None:
        with self._lock:
            if assessment.id in self._assessments:
                raise ValueError(f"Assessment {assessment.id} already exists")
            self._assessments[assessment.id] = assessment

    def get(self, assessment_id: str) -> Assessment | None:
        with self._lock:
            return self._assessments.get(assessment_id)

    def query(self, criteria: AssessmentFilter) -> list[Assessment]:
        with self._lock:
            results = list(self._assessments.values())

        results = _apply_filters(results, criteria)
        results.sort(key=lambda a: a.created_at, reverse=True)

        if criteria.limit > 0:
            results = results[: criteria.limit]
        return results


def _apply_filters(
    assessments: list[Assessment], criteria: AssessmentFilter
) -> list[Assessment]:
    """Apply filter criteria to a list of assessments."""
    results = assessments
    if criteria.service:
        results = [a for a in results if a.service == criteria.service]
    if criteria.kind:
        results = [a for a in results if a.kind == criteria.kind]
    if criteria.producer:
        results = [a for a in results if a.producer == criteria.producer]
    if criteria.from_time:
        results = [a for a in results if a.created_at >= criteria.from_time]
    if criteria.to_time:
        results = [a for a in results if a.created_at <= criteria.to_time]
    return results
