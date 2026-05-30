"""Verdict chain correlation — upstream quality analysis (opensrm-jmy.3).

When Agent B's reversal rate climbs because Agent A's upstream output
quality degraded, walking the OpenSRM service dependency graph won't
find it: the dependency is at the verdict level, not the infrastructure
level. This analyzer adds a second correlation path that walks the
verdict lineage (``lineage.context``) to identify upstream services
whose quality degradation correlates with the breaching service's
reversal rate.

Spec: ``docs/roadmap/NTHLAYER_MISSING_CAPABILITIES_SPEC.md`` § 3.

MVP scope landed:

- ``VerdictChainAnalyzer`` with the four configurable knobs from the
  spec (max_depth, confidence_decay_per_level, min_upstream_representation,
  max_verdicts_per_query).
- ``analyze(service, verdict_store, incident_window, baseline_window)``
  produces a ``VerdictChainResult`` (or ``None`` if no chain root cause
  is identified).
- Recursive traversal up to ``max_depth`` levels with confidence decay
  per level applied multiplicatively.
- Early termination when no upstream meets ``min_upstream_representation``.
- Baseline comparison: average judgment confidence over a configurable
  pre-incident window vs the incident-window average.

Deferred:

- 5-minute baseline cache (the spec § Performance Requirements asks
  for a configurable TTL — for v1.5 baselines are recomputed on each
  call; cache lands when the analyzer wires into a long-running
  correlate worker cycle).
- Wiring into ``CorrelateSessionModule.process_cycle`` so the analyzer
  runs alongside the existing service-dependency correlation; current
  cycle keeps emitting only correlation_snapshot assessments. Wiring
  is a separate change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import structlog

from nthlayer_common.verdicts.models import Verdict
from nthlayer_common.verdicts.store import VerdictFilter, VerdictStore

logger = structlog.get_logger(__name__)

# Outcome statuses that indicate a downstream verdict was reversed —
# i.e. the human or downstream signal disagreed with the agent's call.
# Spec § 3 calls these "reversed/overridden verdicts".
_REVERSED_OUTCOME_STATUSES = frozenset({"overridden"})


@dataclass
class VerdictChainEvidence:
    """One upstream → downstream relationship observed during traversal."""

    downstream_service: str
    downstream_reversal_rate: float
    upstream_service: str
    upstream_confidence_shift: float  # negative = degraded
    upstream_score_degradation: str  # "0.87 → 0.71"
    upstream_representation: float  # 0.0–1.0; share of reversed verdicts referencing this upstream
    sample_verdicts: list[dict[str, str]] = field(default_factory=list)


@dataclass
class VerdictChainResult:
    """Output of a single ``analyze`` call."""

    root_cause: str | None  # upstream service name, or None if no chain root cause
    chain_depth: int  # 0 if no root cause; 1 for direct upstream; 2+ for recursion
    confidence: float  # 0.0–1.0; decayed per level
    evidence: list[VerdictChainEvidence] = field(default_factory=list)
    assessment: str = ""


class VerdictChainAnalyzer:
    """Walk verdict lineage to find upstream quality degradation correlations.

    Tunable knobs from the spec § 3 Configuration block:

    - ``max_depth``: how many levels of recursion (1 = direct upstream;
      2 = upstream-of-upstream; etc.).
    - ``confidence_decay_per_level``: multiplicative decay applied per
      additional depth level. ``0.3`` means each step shrinks confidence
      by 30% (multiplied by ``0.7``).
    - ``min_upstream_representation``: minimum share of reversed
      downstream verdicts that must reference an upstream service for
      that upstream to qualify. Below this, the analyzer early-terminates.
    - ``max_verdicts_per_query``: safety cap on a single verdict-window
      query.
    """

    def __init__(
        self,
        *,
        max_depth: int = 2,
        confidence_decay_per_level: float = 0.3,
        min_upstream_representation: float = 0.1,
        max_verdicts_per_query: int = 10000,
        base_confidence: float = 0.78,
    ) -> None:
        if not (0.0 <= confidence_decay_per_level < 1.0):
            raise ValueError(
                f"confidence_decay_per_level must be in [0.0, 1.0); got {confidence_decay_per_level}"
            )
        if not (0.0 <= min_upstream_representation <= 1.0):
            raise ValueError(
                f"min_upstream_representation must be in [0.0, 1.0]; got {min_upstream_representation}"
            )
        if max_depth < 1:
            raise ValueError(f"max_depth must be >= 1; got {max_depth}")
        self.max_depth = max_depth
        self.confidence_decay_per_level = confidence_decay_per_level
        self.min_upstream_representation = min_upstream_representation
        self.max_verdicts_per_query = max_verdicts_per_query
        self.base_confidence = base_confidence

    def analyze(
        self,
        service: str,
        verdict_store: VerdictStore,
        incident_window: tuple[datetime, datetime],
        baseline_window: tuple[datetime, datetime] | None = None,
    ) -> VerdictChainResult | None:
        """Find an upstream root cause for downstream quality degradation.

        Args:
            service: the breaching service whose reversed verdicts we
                trace upstream from.
            verdict_store: source for verdict + lineage queries.
            incident_window: ``(from, to)`` UTC datetimes — the period
                during which the SLO breach was active.
            baseline_window: optional pre-incident window for upstream
                quality baselines. Defaults to the same duration as the
                incident window, ending immediately before it.

        Returns:
            ``VerdictChainResult`` when an upstream root cause is found,
            else ``None``. ``None`` covers all the early-termination
            paths (no reversed verdicts, no qualifying upstream, no
            quality degradation against baseline).
        """
        baseline_window = baseline_window or self._default_baseline_window(incident_window)

        downstream_verdicts = self._fetch_downstream(service, verdict_store, incident_window)
        if not downstream_verdicts:
            return None

        reversed_verdicts = [
            v for v in downstream_verdicts if self._is_reversed(v)
        ]
        if not reversed_verdicts:
            return None

        downstream_reversal_rate = len(reversed_verdicts) / len(downstream_verdicts)

        result = self._traverse(
            service=service,
            reversed_downstream=reversed_verdicts,
            downstream_reversal_rate=downstream_reversal_rate,
            verdict_store=verdict_store,
            baseline_window=baseline_window,
            depth=1,
        )
        return result

    # ------------------------------------------------------------------ #
    # Internal traversal                                                  #
    # ------------------------------------------------------------------ #

    def _traverse(
        self,
        *,
        service: str,
        reversed_downstream: list[Verdict],
        downstream_reversal_rate: float,
        verdict_store: VerdictStore,
        baseline_window: tuple[datetime, datetime],
        depth: int,
    ) -> VerdictChainResult | None:
        """One level of upstream traversal. Recurses up to ``max_depth``."""
        # Step: extract lineage.context references and group by upstream service.
        upstream_groups = self._group_upstream(reversed_downstream, verdict_store)
        if not upstream_groups:
            return None

        # Step: apply min_upstream_representation threshold.
        total_reversed = len(reversed_downstream)
        qualifying = {
            svc: refs
            for svc, refs in upstream_groups.items()
            if (len(refs) / total_reversed) >= self.min_upstream_representation
        }
        if not qualifying:
            return None

        # Pick the upstream with the highest representation as the
        # primary candidate. Ties broken alphabetically for determinism.
        primary_upstream = sorted(
            qualifying.items(),
            key=lambda kv: (-len(kv[1]), kv[0]),
        )[0][0]
        primary_refs = qualifying[primary_upstream]
        representation = len(primary_refs) / total_reversed

        # Step: compare upstream quality to baseline.
        degradation = self._upstream_degradation(
            primary_upstream, verdict_store, baseline_window, primary_refs,
        )
        if degradation is None:
            # No measurable degradation against baseline → not the root cause.
            return None

        baseline_avg, current_avg = degradation
        confidence = self.base_confidence * (
            (1.0 - self.confidence_decay_per_level) ** (depth - 1)
        )

        sample_verdicts = [
            {"downstream": v.id, "upstream_context": _first_upstream_context(v)}
            for v in reversed_downstream[:5]
            if _first_upstream_context(v) is not None
        ]

        evidence = VerdictChainEvidence(
            downstream_service=service,
            downstream_reversal_rate=round(downstream_reversal_rate, 4),
            upstream_service=primary_upstream,
            upstream_confidence_shift=round(current_avg - baseline_avg, 4),
            upstream_score_degradation=f"{baseline_avg:.2f} → {current_avg:.2f}",
            upstream_representation=round(representation, 4),
            sample_verdicts=sample_verdicts,
        )

        # Recurse if depth budget remains. The recursion target is the
        # upstream service: we treat the upstream's reversed verdicts
        # as the new "downstream" and traverse one more level.
        if depth < self.max_depth:
            upstream_reversed = self._fetch_upstream_reversed(
                primary_upstream, verdict_store, baseline_window
            )
            if upstream_reversed:
                deeper = self._traverse(
                    service=primary_upstream,
                    reversed_downstream=upstream_reversed,
                    downstream_reversal_rate=(
                        len(upstream_reversed) / max(1, len(primary_refs))
                    ),
                    verdict_store=verdict_store,
                    baseline_window=baseline_window,
                    depth=depth + 1,
                )
                if deeper is not None:
                    deeper.evidence.insert(0, evidence)
                    deeper.assessment = self._build_assessment(
                        service, deeper.root_cause or primary_upstream,
                        deeper.confidence, deeper.evidence,
                    )
                    return deeper

        return VerdictChainResult(
            root_cause=primary_upstream,
            chain_depth=depth,
            confidence=round(confidence, 4),
            evidence=[evidence],
            assessment=self._build_assessment(service, primary_upstream, confidence, [evidence]),
        )

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _is_reversed(v: Verdict) -> bool:
        return getattr(v.outcome, "status", None) in _REVERSED_OUTCOME_STATUSES

    def _fetch_downstream(
        self,
        service: str,
        verdict_store: VerdictStore,
        window: tuple[datetime, datetime],
    ) -> list[Verdict]:
        from_time, to_time = window
        return verdict_store.query(
            VerdictFilter(
                subject_service=service,
                from_time=from_time,
                to_time=to_time,
                limit=self.max_verdicts_per_query,
            )
        )

    def _group_upstream(
        self,
        downstream: list[Verdict],
        verdict_store: VerdictStore,
    ) -> dict[str, list[str]]:
        """Return ``{upstream_service: [downstream_id, ...]}`` from ``lineage.context``."""
        groups: dict[str, list[str]] = {}
        for d in downstream:
            for ctx_id in (d.lineage.context or []):
                upstream = verdict_store.get(ctx_id)
                if upstream is None:
                    continue
                upstream_svc = (
                    getattr(upstream.subject, "service", None)
                    or getattr(upstream.subject, "ref", None)
                )
                if not upstream_svc:
                    continue
                groups.setdefault(upstream_svc, []).append(d.id)
        return groups

    def _upstream_degradation(
        self,
        upstream_service: str,
        verdict_store: VerdictStore,
        baseline_window: tuple[datetime, datetime],
        # Reserved for cache invalidation in future revisions:
        upstream_refs: list[str],  # noqa: ARG002
    ) -> tuple[float, float] | None:
        """Return ``(baseline_avg, current_avg)`` if degradation detected, else ``None``."""
        baseline_from, baseline_to = baseline_window
        # Baseline: judgment confidence average over the pre-incident window.
        baseline_verdicts = verdict_store.query(
            VerdictFilter(
                subject_service=upstream_service,
                from_time=baseline_from,
                to_time=baseline_to,
                limit=self.max_verdicts_per_query,
            )
        )
        if not baseline_verdicts:
            return None
        baseline_avg = _confidence_mean(baseline_verdicts)

        # Current: judgment confidence average over a recent window of the same length
        # ending now (approximation: use baseline_to as the cutoff for "current").
        # Caller passed an incident_window; we use baseline_to → baseline_to + window
        # so the comparison is symmetric.
        window_len = baseline_to - baseline_from
        current_from = baseline_to
        current_to = baseline_to + window_len
        current_verdicts = verdict_store.query(
            VerdictFilter(
                subject_service=upstream_service,
                from_time=current_from,
                to_time=current_to,
                limit=self.max_verdicts_per_query,
            )
        )
        if not current_verdicts:
            return None
        current_avg = _confidence_mean(current_verdicts)

        # Degradation = current is meaningfully lower than baseline.
        # 0.05 absolute or 5% relative is a deliberate floor: small noise
        # in the sample means shouldn't trigger a root-cause flag.
        if current_avg >= baseline_avg - 0.05:
            return None

        return baseline_avg, current_avg

    def _fetch_upstream_reversed(
        self,
        upstream_service: str,
        verdict_store: VerdictStore,
        baseline_window: tuple[datetime, datetime],
    ) -> list[Verdict]:
        """Reversed verdicts on the upstream service for recursion."""
        baseline_to = baseline_window[1]
        window_len = baseline_window[1] - baseline_window[0]
        candidates = verdict_store.query(
            VerdictFilter(
                subject_service=upstream_service,
                from_time=baseline_to,
                to_time=baseline_to + window_len,
                limit=self.max_verdicts_per_query,
            )
        )
        return [v for v in candidates if self._is_reversed(v)]

    @staticmethod
    def _default_baseline_window(
        incident_window: tuple[datetime, datetime],
    ) -> tuple[datetime, datetime]:
        """Default baseline = same length as incident, ending immediately before it."""
        from_time, to_time = incident_window
        window_len = to_time - from_time
        return (from_time - window_len, from_time)

    @staticmethod
    def _build_assessment(
        downstream_service: str,
        upstream_service: str,
        confidence: float,
        evidence: list[VerdictChainEvidence],
    ) -> str:
        if not evidence:
            return ""
        primary = evidence[0]
        return (
            f"{downstream_service}'s judgment quality degradation correlates "
            f"with declining output quality from {upstream_service}. "
            f"{primary.upstream_representation:.0%} of recent reversed verdicts "
            f"reference {upstream_service} verdicts; upstream confidence shifted "
            f"by {primary.upstream_confidence_shift:+.2f} "
            f"({primary.upstream_score_degradation}). "
            f"Chain confidence {confidence:.2f}."
        )


def _first_upstream_context(v: Verdict) -> str | None:
    if not v.lineage.context:
        return None
    return v.lineage.context[0]


def _confidence_mean(verdicts: list[Verdict]) -> float:
    """Mean judgment.confidence across a list, ignoring missing values."""
    values = [
        v.judgment.confidence for v in verdicts
        if getattr(v.judgment, "confidence", None) is not None
    ]
    if not values:
        return 0.0
    return sum(values) / len(values)
