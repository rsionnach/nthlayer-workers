"""Tests for the verdict chain correlation analyzer (opensrm-jmy.3).

Covers the 5 test scenarios listed in
docs/roadmap/NTHLAYER_MISSING_CAPABILITIES_SPEC.md § 3.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from nthlayer_common.verdicts.models import (
    Judgment,
    Lineage,
    Outcome,
    Producer,
    Subject,
    Verdict,
)
from nthlayer_common.verdicts.store import MemoryStore

from nthlayer_workers.correlate.verdict_chain import (
    VerdictChainAnalyzer,
    VerdictChainResult,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _verdict(
    *,
    service: str,
    timestamp: datetime,
    confidence: float = 0.85,
    overridden: bool = False,
    context: list[str] | None = None,
    vid: str | None = None,
) -> Verdict:
    """Build a minimal valid Verdict."""
    return Verdict(
        id=vid or f"vrd-{uuid.uuid4().hex[:8]}",
        version=1,
        timestamp=timestamp,
        producer=Producer(system=service),
        subject=Subject(type="evaluation", ref=service, summary="test", service=service),
        judgment=Judgment(action="flag", confidence=confidence, reasoning=""),
        outcome=Outcome(status="overridden" if overridden else "pending"),
        lineage=Lineage(context=list(context or [])),
        service=service,
    )


def _fresh_store() -> MemoryStore:
    return MemoryStore()


def _seed_chain_two_level(
    store: MemoryStore,
    *,
    incident_start: datetime,
    upstream_baseline_conf: float,
    upstream_current_conf: float,
    downstream_overridden: int,
    downstream_total: int,
    upstream_service: str = "agent-a",
    downstream_service: str = "agent-b",
) -> None:
    """Seed a B → A chain.

    Baseline window: 1h before incident_start.
    Current window: incident_start onwards.

    Each downstream verdict references one upstream verdict in
    ``lineage.context``. The downstream verdicts split into overridden
    and pending counts so the analyzer's reversed-rate logic exercises.
    """
    baseline_window_start = incident_start - timedelta(hours=1)
    # Namespace IDs by the (upstream, downstream) pair so multiple seed
    # calls in the same store don't collide. MemoryStore.put overwrites
    # by id; without this, layer-2 seeds in the 3-deep test would clobber
    # layer-1 verdicts that share generic IDs.
    nspc = f"{upstream_service}-{downstream_service}"

    # Baseline upstream verdicts (for the pre-incident average).
    for i in range(20):
        store.put(_verdict(
            service=upstream_service,
            timestamp=baseline_window_start + timedelta(minutes=i * 2),
            confidence=upstream_baseline_conf,
            vid=f"{nspc}-upstream-baseline-{i}",
        ))

    # Current-window upstream verdicts (post-incident-start; what the
    # downstream verdicts reference).
    upstream_ids: list[str] = []
    for i in range(downstream_total):
        upstream_id = f"{nspc}-upstream-current-{i}"
        store.put(_verdict(
            service=upstream_service,
            timestamp=incident_start + timedelta(seconds=i * 30),
            confidence=upstream_current_conf,
            vid=upstream_id,
        ))
        upstream_ids.append(upstream_id)

    # Downstream verdicts referencing the upstream in lineage.context.
    for i in range(downstream_total):
        store.put(_verdict(
            service=downstream_service,
            timestamp=incident_start + timedelta(seconds=i * 30 + 5),
            confidence=0.6,
            overridden=(i < downstream_overridden),
            context=[upstream_ids[i]],
            vid=f"{nspc}-downstream-{i}",
        ))


def _incident_window(
    incident_start: datetime, length: timedelta = timedelta(hours=1)
) -> tuple[datetime, datetime]:
    return incident_start, incident_start + length


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


class TestAnalyzerConfig:
    def test_invalid_decay_raises(self):
        with pytest.raises(ValueError, match="confidence_decay_per_level"):
            VerdictChainAnalyzer(confidence_decay_per_level=1.0)
        with pytest.raises(ValueError, match="confidence_decay_per_level"):
            VerdictChainAnalyzer(confidence_decay_per_level=-0.1)

    def test_invalid_min_representation_raises(self):
        with pytest.raises(ValueError, match="min_upstream_representation"):
            VerdictChainAnalyzer(min_upstream_representation=1.5)

    def test_invalid_max_depth_raises(self):
        with pytest.raises(ValueError, match="max_depth"):
            VerdictChainAnalyzer(max_depth=0)


# ---------------------------------------------------------------------------
# Spec § 3 test scenarios
# ---------------------------------------------------------------------------


class TestSpecScenarios:
    @pytest.fixture
    def incident_start(self):
        return datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)

    def test_1_upstream_degraded_finds_root_cause(self, incident_start):
        """Spec 1: Agent B degrades AND Agent A degrades → identifies Agent A."""
        store = _fresh_store()
        _seed_chain_two_level(
            store,
            incident_start=incident_start,
            upstream_baseline_conf=0.87,
            upstream_current_conf=0.71,  # ~16pp drop, well above 5pp floor
            downstream_overridden=10,
            downstream_total=12,
        )

        analyzer = VerdictChainAnalyzer(max_depth=1)
        result = analyzer.analyze("agent-b", store, _incident_window(incident_start))

        assert result is not None
        assert result.root_cause == "agent-a"
        assert result.chain_depth == 1
        assert result.confidence == pytest.approx(analyzer.base_confidence, abs=0.01)
        assert len(result.evidence) == 1
        ev = result.evidence[0]
        assert ev.downstream_service == "agent-b"
        assert ev.upstream_service == "agent-a"
        assert ev.upstream_confidence_shift < 0  # degraded
        assert ev.upstream_representation > 0.5  # majority of reversed verdicts

    def test_2_upstream_fine_no_root_cause(self, incident_start):
        """Spec 2: Agent B degrades but Agent A is steady → returns None."""
        store = _fresh_store()
        _seed_chain_two_level(
            store,
            incident_start=incident_start,
            upstream_baseline_conf=0.85,
            upstream_current_conf=0.86,  # no degradation
            downstream_overridden=10,
            downstream_total=12,
        )

        analyzer = VerdictChainAnalyzer(max_depth=1)
        result = analyzer.analyze("agent-b", store, _incident_window(incident_start))
        assert result is None

    def test_3_three_deep_chain_with_decay(self, incident_start):
        """Spec 3: C → B → A with max_depth=3 → reaches A with decay applied.

        Baseline+current upstream verdicts are seeded for both A and B
        so the analyzer has data to compare at each level.
        """
        store = _fresh_store()

        # Layer 1: B → A
        _seed_chain_two_level(
            store,
            incident_start=incident_start,
            upstream_baseline_conf=0.90,
            upstream_current_conf=0.70,
            downstream_overridden=10,
            downstream_total=12,
            upstream_service="agent-a",
            downstream_service="agent-b",
        )

        # Layer 2: C → B (treats agent-b as the upstream for agent-c)
        _seed_chain_two_level(
            store,
            incident_start=incident_start,
            upstream_baseline_conf=0.88,
            upstream_current_conf=0.65,
            downstream_overridden=10,
            downstream_total=12,
            upstream_service="agent-b",
            downstream_service="agent-c",
        )

        analyzer = VerdictChainAnalyzer(
            max_depth=3, confidence_decay_per_level=0.3,
        )
        result = analyzer.analyze("agent-c", store, _incident_window(incident_start))

        assert result is not None
        assert result.chain_depth >= 2
        # confidence after 1 decay step = base × 0.7; after 2 = base × 0.49.
        assert result.confidence < analyzer.base_confidence

    def test_4_no_lineage_returns_none(self, incident_start):
        """Spec 5: Service with no verdict lineage → analyzer returns None gracefully."""
        store = _fresh_store()
        # Reversed downstream verdicts but no lineage.context.
        for i in range(10):
            store.put(_verdict(
                service="legacy-svc",
                timestamp=incident_start + timedelta(minutes=i),
                overridden=True,
                context=[],
            ))

        analyzer = VerdictChainAnalyzer()
        result = analyzer.analyze("legacy-svc", store, _incident_window(incident_start))
        assert result is None


# ---------------------------------------------------------------------------
# Threshold + early-termination behaviour
# ---------------------------------------------------------------------------


class TestEarlyTermination:
    @pytest.fixture
    def incident_start(self):
        return datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)

    def test_min_representation_filters_diffuse_upstream(self, incident_start):
        """Reversed verdicts with many one-off upstream contexts → no qualifying upstream."""
        store = _fresh_store()
        # 10 reversed downstream verdicts each referencing a unique upstream.
        # Each upstream has 1/10 = 10% representation, below the 11% threshold.
        for i in range(10):
            upstream_id = f"upstream-{i}"
            store.put(_verdict(
                service=f"svc-upstream-{i}",
                timestamp=incident_start + timedelta(seconds=i),
                vid=upstream_id,
            ))
            store.put(_verdict(
                service="downstream",
                timestamp=incident_start + timedelta(seconds=i + 1),
                overridden=True,
                context=[upstream_id],
            ))

        analyzer = VerdictChainAnalyzer(min_upstream_representation=0.11)
        result = analyzer.analyze("downstream", store, _incident_window(incident_start))
        assert result is None

    def test_no_reversed_verdicts_returns_none(self, incident_start):
        store = _fresh_store()
        for i in range(5):
            store.put(_verdict(
                service="downstream",
                timestamp=incident_start + timedelta(seconds=i),
                overridden=False,
            ))

        analyzer = VerdictChainAnalyzer()
        result = analyzer.analyze("downstream", store, _incident_window(incident_start))
        assert result is None

    def test_no_downstream_verdicts_returns_none(self, incident_start):
        store = _fresh_store()
        analyzer = VerdictChainAnalyzer()
        result = analyzer.analyze("downstream", store, _incident_window(incident_start))
        assert result is None


# ---------------------------------------------------------------------------
# VerdictChainResult shape
# ---------------------------------------------------------------------------


class TestResultShape:
    @pytest.fixture
    def incident_start(self):
        return datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)

    def test_result_contains_assessment_string(self, incident_start):
        store = _fresh_store()
        _seed_chain_two_level(
            store,
            incident_start=incident_start,
            upstream_baseline_conf=0.90,
            upstream_current_conf=0.65,
            downstream_overridden=10,
            downstream_total=12,
        )
        analyzer = VerdictChainAnalyzer(max_depth=1)
        result = analyzer.analyze("agent-b", store, _incident_window(incident_start))
        assert isinstance(result, VerdictChainResult)
        assert result.assessment
        assert "agent-a" in result.assessment
        assert "agent-b" in result.assessment

    def test_evidence_carries_sample_verdicts(self, incident_start):
        store = _fresh_store()
        _seed_chain_two_level(
            store,
            incident_start=incident_start,
            upstream_baseline_conf=0.90,
            upstream_current_conf=0.65,
            downstream_overridden=10,
            downstream_total=12,
        )
        analyzer = VerdictChainAnalyzer(max_depth=1)
        result = analyzer.analyze("agent-b", store, _incident_window(incident_start))
        assert result is not None
        ev = result.evidence[0]
        assert len(ev.sample_verdicts) > 0
        for sample in ev.sample_verdicts:
            assert "downstream" in sample
            assert "upstream_context" in sample
