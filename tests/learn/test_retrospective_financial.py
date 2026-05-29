"""Tests for retrospective financial-impact computation (opensrm-jmy.1).

Exercises the spec § 1 retrospective integration: when a blast-radius
service declares an ``outcomes`` block, the retrospective carries a
``financial_impact`` field with currency, decisions, failure mode, and
volume source. Tests the pure-function ``_compute_financial_impact``
directly — full ``build_retrospective`` integration is exercised by
the existing module tests.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from nthlayer_workers.learn.retrospective import _compute_financial_impact


@pytest.fixture
def specs_dir(tmp_path: Path) -> Path:
    spec = textwrap.dedent("""
        apiVersion: opensrm.nthlayer.io/v2
        kind: ServiceManifest
        metadata:
          name: fraud-detect
          labels:
            tier: critical
            type: ai-gate
        spec:
          owner:
            group: group:default/payments
          service:
            name: fraud-detect
          outcomes:
            decision_value:
              correct: 138.50
              currency: EUR
              false_negative:
                cost: 2400
                category: financial_loss
              false_positive:
                cost: 0
                category: friction
            volume:
              estimated_daily_decisions: 12000
    """).strip()
    (tmp_path / "fraud-detect.yaml").write_text(spec)
    return tmp_path


def test_metric_path_uses_per_service_breach_count(specs_dir: Path):
    # Spec §1 scenario 1 proxy: when breach_counts_by_service[svc] > 0,
    # use it as that service's impacted-decisions count and tag
    # volume_source="metric".
    result = _compute_financial_impact(
        blast_radius=["fraud-detect"],
        duration_minutes=30.0,
        breach_counts_by_service={"fraud-detect": 340},
        specs_dir=str(specs_dir),
    )
    assert result is not None
    assert result["estimated"] == pytest.approx(816000.0, abs=0.01)
    assert result["currency"] == "EUR"
    assert result["decisions_affected"] == 340
    assert result["failure_mode"] == "false_negative"
    assert result["volume_source"] == "metric"


def test_spec_estimate_fallback(specs_dir: Path):
    # Spec §1 scenario 2: no breach signal for the service → fall back
    # to estimated_daily_decisions prorated to incident duration.
    # 30m / 24h * 12000 = 250 decisions.
    result = _compute_financial_impact(
        blast_radius=["fraud-detect"],
        duration_minutes=30.0,
        breach_counts_by_service={},
        specs_dir=str(specs_dir),
    )
    assert result is not None
    assert result["volume_source"] == "spec_estimate"
    assert result["decisions_affected"] == 250
    assert result["estimated"] == pytest.approx(250 * 2400.0, abs=0.01)


def test_no_outcomes_returns_none(tmp_path: Path):
    # Spec §1 scenario 3: a service without outcomes → no error,
    # no financial fields. We assert by feeding a minimal manifest
    # and verifying the function returns None cleanly.
    spec = textwrap.dedent("""
        apiVersion: opensrm.nthlayer.io/v2
        kind: ServiceManifest
        metadata:
          name: silent-svc
          labels:
            tier: standard
            type: api
        spec:
          owner:
            group: group:default/team-a
          service:
            name: silent-svc
    """).strip()
    (tmp_path / "silent-svc.yaml").write_text(spec)
    result = _compute_financial_impact(
        blast_radius=["silent-svc"],
        duration_minutes=30.0,
        breach_counts_by_service={},
        specs_dir=str(tmp_path),
    )
    assert result is None


def test_blast_radius_unmatched_service_returns_none(specs_dir: Path):
    # Service in blast_radius but no matching manifest → return None.
    result = _compute_financial_impact(
        blast_radius=["unknown-service"],
        duration_minutes=30.0,
        breach_counts_by_service={"unknown-service": 100},
        specs_dir=str(specs_dir),
    )
    assert result is None


def test_blast_radius_dict_shape_supported(specs_dir: Path):
    # Correlate emits blast_radius as either string list or
    # [{"service": ...}] dict list — both must work.
    result = _compute_financial_impact(
        blast_radius=[{"service": "fraud-detect"}],
        duration_minutes=30.0,
        breach_counts_by_service={"fraud-detect": 10},
        specs_dir=str(specs_dir),
    )
    assert result is not None
    assert result["currency"] == "EUR"


def test_no_specs_dir_returns_none():
    assert _compute_financial_impact(
        blast_radius=["x"],
        duration_minutes=30.0,
        breach_counts_by_service={},
        specs_dir=None,
    ) is None


def test_empty_blast_radius_returns_none(specs_dir: Path):
    assert _compute_financial_impact(
        blast_radius=[],
        duration_minutes=30.0,
        breach_counts_by_service={"fraud-detect": 10},
        specs_dir=str(specs_dir),
    ) is None


def test_duplicate_manifest_for_same_service_deduped(specs_dir: Path):
    # Template fork / env overlay may put two manifests with the same
    # metadata.name under one specs_dir. Without dedup they would
    # double-count the per-service breach total.
    duplicate = (specs_dir / "fraud-detect.yaml").read_text()
    (specs_dir / "fraud-detect-overlay.yaml").write_text(duplicate)
    result = _compute_financial_impact(
        blast_radius=["fraud-detect"],
        duration_minutes=30.0,
        breach_counts_by_service={"fraud-detect": 100},
        specs_dir=str(specs_dir),
    )
    assert result is not None
    assert result["decisions_affected"] == 100
    assert result["estimated"] == pytest.approx(100 * 2400.0, abs=0.01)


def test_no_duration_and_no_breaches_returns_none(specs_dir: Path):
    # Neither metric path nor spec_estimate path can produce a figure;
    # surface that explicitly rather than emitting a misleading None.
    result = _compute_financial_impact(
        blast_radius=["fraud-detect"],
        duration_minutes=0.0,
        breach_counts_by_service={},
        specs_dir=str(specs_dir),
    )
    assert result is None


class TestDeclaredDependenciesByService:
    """opensrm-jmy.21: build_retrospective() populates
    ``retro.metadata.custom["declared_dependencies_by_service"]``.

    Exercised via the two pure helpers (``_load_manifests_from_specs`` +
    ``_extract_declared_dependencies``) the public function delegates to,
    keeping these tests free of the full verdict-store + verdict-lineage
    setup that ``build_retrospective`` requires. The mapping from helper
    pair → ``retro.metadata.custom[...]`` is a single line in
    ``build_retrospective`` (line 162).
    """

    def test_declared_dependencies_extracted_from_manifests(self, tmp_path: Path):
        from nthlayer_workers.learn.retrospective import (
            _extract_declared_dependencies,
            _load_manifests_from_specs,
        )

        svc_a = textwrap.dedent("""
            apiVersion: opensrm.nthlayer.io/v2
            kind: ServiceManifest
            metadata: {name: svc-a, labels: {tier: critical, type: api}}
            spec:
              owner: {group: group:default/team-a}
              service: {name: svc-a}
              dependencies:
                - service: component:default/dep-one
                  type: api
                - service: component:default/dep-two
                  type: database
        """).strip()
        svc_b = textwrap.dedent("""
            apiVersion: opensrm.nthlayer.io/v2
            kind: ServiceManifest
            metadata: {name: svc-b, labels: {tier: standard, type: api}}
            spec:
              owner: {group: group:default/team-b}
              service: {name: svc-b}
        """).strip()
        (tmp_path / "svc-a.yaml").write_text(svc_a)
        (tmp_path / "svc-b.yaml").write_text(svc_b)

        loaded = _load_manifests_from_specs(str(tmp_path))
        declared = _extract_declared_dependencies(loaded)

        assert declared == {
            "svc-a": ["dep-one", "dep-two"],
            "svc-b": [],
        }

    def test_declared_dependencies_empty_when_specs_dir_none(self):
        from nthlayer_workers.learn.retrospective import (
            _extract_declared_dependencies,
            _load_manifests_from_specs,
        )

        loaded = _load_manifests_from_specs(None)
        assert _extract_declared_dependencies(loaded) == {}

    def test_declared_dependencies_empty_list_for_no_deps_block(self, tmp_path: Path):
        """Services without ``dependencies:`` yield an empty list, never an absent key.

        The absence of declared deps is itself information downstream consumers
        (jmy.21 add_dependency) want — silently omitting the key would make a
        service with `dependencies: []` indistinguishable from a service that
        wasn't loaded.
        """
        from nthlayer_workers.learn.retrospective import (
            _extract_declared_dependencies,
            _load_manifests_from_specs,
        )

        spec = textwrap.dedent("""
            apiVersion: opensrm.nthlayer.io/v2
            kind: ServiceManifest
            metadata: {name: silent-svc, labels: {tier: standard, type: api}}
            spec:
              owner: {group: group:default/team-a}
              service: {name: silent-svc}
        """).strip()
        (tmp_path / "silent-svc.yaml").write_text(spec)

        loaded = _load_manifests_from_specs(str(tmp_path))
        declared = _extract_declared_dependencies(loaded)

        assert "silent-svc" in declared
        assert declared["silent-svc"] == []


def test_multi_service_metric_path_attributes_per_service(tmp_path: Path):
    # Critical correctness check: with 2 services in blast_radius, the
    # metric path must attribute each service's own breach count, not
    # multiply the total by every cost.
    spec_a = textwrap.dedent("""
        apiVersion: opensrm.nthlayer.io/v2
        kind: ServiceManifest
        metadata: {name: svc-a, labels: {tier: critical, type: ai-gate}}
        spec:
          owner: {group: group:default/team-a}
          service: {name: svc-a}
          outcomes:
            decision_value:
              correct: 10
              currency: EUR
              false_negative: {cost: 100, category: financial_loss}
    """).strip()
    spec_b = textwrap.dedent("""
        apiVersion: opensrm.nthlayer.io/v2
        kind: ServiceManifest
        metadata: {name: svc-b, labels: {tier: critical, type: ai-gate}}
        spec:
          owner: {group: group:default/team-b}
          service: {name: svc-b}
          outcomes:
            decision_value:
              correct: 10
              currency: EUR
              false_negative: {cost: 50, category: financial_loss}
    """).strip()
    (tmp_path / "svc-a.yaml").write_text(spec_a)
    (tmp_path / "svc-b.yaml").write_text(spec_b)
    result = _compute_financial_impact(
        blast_radius=["svc-a", "svc-b"],
        duration_minutes=30.0,
        breach_counts_by_service={"svc-a": 10, "svc-b": 4},
        specs_dir=str(tmp_path),
    )
    assert result is not None
    # 10 × 100 (svc-a) + 4 × 50 (svc-b) = 1200, NOT 14 × 100 + 14 × 50.
    assert result["estimated"] == pytest.approx(1200.0, abs=0.01)
    assert result["decisions_affected"] == 14
