"""Models for contract verification."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class MetricSource(Enum):
    """Source of a declared metric in service.yaml."""

    SLO_INDICATOR = "slo_indicator"
    OBSERVABILITY = "observability"
    ALERT = "alert"


@dataclass
class DeclaredMetric:
    """A metric declared in service.yaml."""

    name: str
    source: MetricSource
    query: str | None = None
    resource_name: str | None = None

    @property
    def is_critical(self) -> bool:
        """SLO and alert metrics are critical for service reliability."""
        return self.source in (MetricSource.SLO_INDICATOR, MetricSource.ALERT)


@dataclass
class MetricContract:
    """The metric contract for a service."""

    service_name: str
    metrics: list[DeclaredMetric] = field(default_factory=list)

    @property
    def critical_metrics(self) -> list[DeclaredMetric]:
        return [m for m in self.metrics if m.is_critical]

    @property
    def optional_metrics(self) -> list[DeclaredMetric]:
        return [m for m in self.metrics if not m.is_critical]

    @property
    def unique_metric_names(self) -> set[str]:
        return {m.name for m in self.metrics}


@dataclass
class VerificationResult:
    """Result of verifying a single metric."""

    metric: DeclaredMetric
    exists: bool
    error: str | None = None
    sample_labels: dict | None = None

    @property
    def is_critical_failure(self) -> bool:
        return not self.exists and self.metric.is_critical


@dataclass
class ContractVerificationResult:
    """Result of verifying the entire metric contract."""

    service_name: str
    target_url: str
    results: list[VerificationResult] = field(default_factory=list)

    @property
    def all_verified(self) -> bool:
        return all(r.exists for r in self.results)

    @property
    def critical_verified(self) -> bool:
        return all(r.exists for r in self.results if r.metric.is_critical)

    @property
    def missing_critical(self) -> list[VerificationResult]:
        return [r for r in self.results if r.is_critical_failure]

    @property
    def missing_optional(self) -> list[VerificationResult]:
        return [r for r in self.results if not r.exists and not r.metric.is_critical]

    @property
    def verified_count(self) -> int:
        return sum(1 for r in self.results if r.exists)

    @property
    def exit_code(self) -> int:
        """Exit code for CI/CD: 0=all verified, 1=optional missing, 2=critical missing."""
        if self.missing_critical:
            return 2
        if self.missing_optional:
            return 1
        return 0
