"""Tests for the Learn → Spec recommendation engine (opensrm-jmy.2)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
import yaml

from nthlayer_workers.learn.recommendations import (
    Recommendation,
    SpecRecommendation,
    analyze_incident,
)


# ---------------------------------------------------------------------------
# SpecRecommendation model
# ---------------------------------------------------------------------------


class TestSpecRecommendationModel:
    def test_default_requires_human_review_true(self):
        s = SpecRecommendation(
            incident="INC-1", generated_by="nthlayer-learn",
            generated_at=datetime.now(timezone.utc),
            confidence=0.5, recommendations=[],
        )
        assert s.requires_human_review is True

    def test_explicit_false_rejected(self):
        with pytest.raises(ValueError, match="requires_human_review"):
            SpecRecommendation(
                incident="INC-1", generated_by="nthlayer-learn",
                generated_at=datetime.now(timezone.utc),
                confidence=0.5, recommendations=[],
                requires_human_review=False,
            )

    def test_confidence_bounds_enforced(self):
        with pytest.raises(ValueError, match="confidence"):
            SpecRecommendation(
                incident="INC-1", generated_by="nthlayer-learn",
                generated_at=datetime.now(timezone.utc),
                confidence=1.5, recommendations=[],
            )
        with pytest.raises(ValueError, match="confidence"):
            SpecRecommendation(
                incident="INC-1", generated_by="nthlayer-learn",
                generated_at=datetime.now(timezone.utc),
                confidence=-0.1, recommendations=[],
            )

    def test_to_yaml_shape(self):
        s = SpecRecommendation(
            incident="INC-X",
            generated_by="nthlayer-learn",
            generated_at=datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc),
            confidence=0.8,
            recommendations=[
                Recommendation(
                    id="rec-test00000001",
                    service="svc-a", type="tighten_slo",
                    field="spec.slos.availability.target",
                    current_value=99.9, proposed_value=99.95,
                    rationale="x", confidence=0.7,
                ),
            ],
        )
        out = yaml.safe_load(s.to_yaml())
        assert out["apiVersion"] == "opensrm.io/v1"
        assert out["kind"] == "SpecRecommendation"
        assert out["metadata"]["incident"] == "INC-X"
        assert out["metadata"]["confidence"] == 0.8
        assert out["metadata"]["requires_human_review"] is True
        assert len(out["recommendations"]) == 1
        assert out["recommendations"][0]["type"] == "tighten_slo"

    def test_to_yaml_drops_empty_optional_fields(self):
        """Empty optional fields stay out of the YAML so it matches the spec example."""
        s = SpecRecommendation(
            incident="INC-Y", generated_by="nthlayer-learn",
            generated_at=datetime.now(timezone.utc),
            confidence=0.5,
            recommendations=[
                Recommendation(
                    id="rec-test00000002",
                    service="svc", type="tighten_slo",
                    rationale="x", proposed_value=99.95,
                    # field, current_value, financial_impact, evidence omitted
                ),
            ],
        )
        out = yaml.safe_load(s.to_yaml())
        rec = out["recommendations"][0]
        assert "financial_impact" not in rec
        assert "evidence" not in rec


# ---------------------------------------------------------------------------
# analyze_incident: tighten_slo
# ---------------------------------------------------------------------------


def _retrospective_with_breach(
    *,
    service: str = "fraud-detect",
    slo_name: str = "reversal_rate",
    target: float = 98.5,
    current: float = 92.0,
    root_causes: list[dict] | None = None,
) -> dict:
    return {
        "evaluations": [
            {
                "service": service,
                "slo_name": slo_name,
                "slo_type": "judgment",
                "breach": True,
                "target": target,
                "current_value": current,
            },
        ],
        "incident_custom": {"root_causes": root_causes or []},
    }


class TestTightenSloRecommendation:
    def test_severe_breach_produces_tighten_slo(self):
        # gap = (98.5 - 92.0) / (100 - 98.5) = 433% — well above the 80% threshold.
        retro = _retrospective_with_breach(target=98.5, current=92.0)
        result = analyze_incident(retro, "INC-1")
        types = {r.type for r in result.recommendations}
        assert "tighten_slo" in types

    def test_proposed_value_is_between_current_and_target(self):
        retro = _retrospective_with_breach(target=98.5, current=92.0)
        result = analyze_incident(retro, "INC-1")
        rec = next(r for r in result.recommendations if r.type == "tighten_slo")
        # Mid-point heuristic: 92.0 + (98.5 - 92.0) * 0.5 = 95.25
        assert 92.0 < rec.proposed_value < 98.5
        assert rec.proposed_value == pytest.approx(95.25, abs=0.01)

    def test_mild_breach_below_threshold_skipped(self):
        # gap = (99.9 - 99.85) / (100 - 99.9) = 50% — below 80% threshold.
        retro = _retrospective_with_breach(target=99.9, current=99.85)
        result = analyze_incident(retro, "INC-2")
        types = {r.type for r in result.recommendations}
        assert "tighten_slo" not in types

    def test_classical_slo_not_tightened(self):
        """Only judgment SLOs get tighten_slo; classical ones are out of scope here."""
        retro = {
            "evaluations": [{
                "service": "svc", "slo_name": "availability",
                "slo_type": "classical", "breach": True,
                "target": 99.9, "current_value": 80.0,
            }],
            "incident_custom": {},
        }
        result = analyze_incident(retro, "INC-3")
        assert all(r.type != "tighten_slo" for r in result.recommendations)

    def test_field_path_targets_slo_name(self):
        retro = _retrospective_with_breach(slo_name="reversal_rate")
        result = analyze_incident(retro, "INC-4")
        rec = next(r for r in result.recommendations if r.type == "tighten_slo")
        assert rec.field == "spec.slos.reversal_rate.target"

    def test_target_at_100_skipped(self):
        """A 100% target has zero error budget — no tightening makes sense."""
        retro = _retrospective_with_breach(target=100.0, current=92.0)
        result = analyze_incident(retro, "INC-5")
        assert all(r.type != "tighten_slo" for r in result.recommendations)


# ---------------------------------------------------------------------------
# analyze_incident: add_deploy_gate
# ---------------------------------------------------------------------------


class TestAddDeployGateRecommendation:
    def test_deploy_root_cause_produces_deploy_gate(self):
        retro = _retrospective_with_breach(
            root_causes=[{"type": "deploy", "service": "fraud-detect"}],
        )
        result = analyze_incident(retro, "INC-6")
        gates = [r for r in result.recommendations if r.type == "add_deploy_gate"]
        assert len(gates) == 1

    def test_model_regression_root_cause_produces_deploy_gate(self):
        retro = _retrospective_with_breach(
            root_causes=[{"type": "model_regression", "service": "fraud-detect"}],
        )
        result = analyze_incident(retro, "INC-7")
        types = {r.type for r in result.recommendations}
        assert "add_deploy_gate" in types

    def test_non_change_root_cause_no_deploy_gate(self):
        retro = _retrospective_with_breach(
            root_causes=[{"type": "infrastructure_failure", "service": "fraud-detect"}],
        )
        result = analyze_incident(retro, "INC-8")
        types = {r.type for r in result.recommendations}
        assert "add_deploy_gate" not in types

    def test_deploy_gate_proposes_block_on_breached_slo(self):
        retro = _retrospective_with_breach(
            root_causes=[{"type": "deploy", "service": "fraud-detect"}],
        )
        result = analyze_incident(retro, "INC-9")
        gate = next(r for r in result.recommendations if r.type == "add_deploy_gate")
        assert gate.proposed_value["enabled"] is True
        assert gate.proposed_value["block_on"] == "reversal_rate"
        assert gate.proposed_value["threshold"] == 98.5

    def test_deploy_gate_without_breach_uses_placeholder(self):
        """Change-shaped root cause without an SLO breach → operator-fills threshold."""
        retro = {
            "evaluations": [],
            "incident_custom": {
                "root_causes": [{"type": "deploy", "service": "svc-X"}],
            },
        }
        result = analyze_incident(retro, "INC-10")
        gate = next(r for r in result.recommendations if r.type == "add_deploy_gate")
        assert gate.proposed_value["block_on"] == "judgment_slo"
        assert "operator" in gate.proposed_value["threshold"].lower()
        # Lower confidence when we couldn't anchor the threshold.
        assert gate.confidence < 0.5


# ---------------------------------------------------------------------------
# analyze_incident: aggregate behaviour
# ---------------------------------------------------------------------------


class TestAnalyzeIncidentAggregate:
    def test_no_breach_no_recommendations(self):
        retro = {"evaluations": [], "incident_custom": {}}
        result = analyze_incident(retro, "INC-EMPTY")
        assert result.recommendations == []
        assert result.confidence == 0.0

    def test_combined_recommendations(self):
        """A severe breach + change root cause → both tighten_slo and add_deploy_gate."""
        retro = _retrospective_with_breach(
            target=98.5, current=92.0,
            root_causes=[{"type": "model_deploy", "service": "fraud-detect"}],
        )
        result = analyze_incident(retro, "INC-COMBO")
        types = sorted(r.type for r in result.recommendations)
        assert types == ["add_deploy_gate", "tighten_slo"]

    def test_overall_confidence_is_average(self):
        retro = _retrospective_with_breach(
            target=98.5, current=92.0,
            root_causes=[{"type": "deploy", "service": "fraud-detect"}],
        )
        result = analyze_incident(retro, "INC-CONF")
        # tighten_slo = 0.7, add_deploy_gate = 0.65 → mean = 0.675 → 0.68
        assert result.confidence == pytest.approx(0.68, abs=0.01)

    def test_default_generated_by(self):
        retro = _retrospective_with_breach()
        result = analyze_incident(retro, "INC-1")
        assert result.generated_by == "nthlayer-learn"

    def test_custom_generated_by(self):
        retro = _retrospective_with_breach()
        result = analyze_incident(retro, "INC-1", generated_by="custom")
        assert result.generated_by == "custom"

    def test_yaml_round_trip(self):
        """Analyse → YAML → re-load yields the same shape."""
        retro = _retrospective_with_breach(
            target=98.5, current=92.0,
            root_causes=[{"type": "deploy", "service": "fraud-detect"}],
        )
        result = analyze_incident(retro, "INC-RT")
        loaded = yaml.safe_load(result.to_yaml())
        assert loaded["metadata"]["incident"] == "INC-RT"
        assert len(loaded["recommendations"]) == len(result.recommendations)
        for orig, lit in zip(result.recommendations, loaded["recommendations"]):
            assert orig.type == lit["type"]
            assert orig.service == lit["service"]


# ---------------------------------------------------------------------------
# jmy.6: deterministic rec-<12-char-sha256-hex> id
# ---------------------------------------------------------------------------


class TestRecommendationId:
    """jmy.6: deterministic rec-<12-char-sha256-hex> id."""

    def test_compute_id_is_deterministic(self):
        from nthlayer_workers.learn.recommendations import compute_rec_id

        id1 = compute_rec_id("inc-2026-05-21-001", "tighten_slo", "spec.slos.judgment.target")
        id2 = compute_rec_id("inc-2026-05-21-001", "tighten_slo", "spec.slos.judgment.target")
        assert id1 == id2

    def test_compute_id_format(self):
        from nthlayer_workers.learn.recommendations import compute_rec_id

        rec_id = compute_rec_id("inc-2026-05-21-001", "tighten_slo", "spec.slos.judgment.target")
        assert rec_id.startswith("rec-")
        assert len(rec_id) == 16  # "rec-" + 12 hex chars
        hex_part = rec_id[4:]
        assert all(c in "0123456789abcdef" for c in hex_part)

    def test_compute_id_changes_per_input(self):
        from nthlayer_workers.learn.recommendations import compute_rec_id

        base = compute_rec_id("inc-A", "tighten_slo", "spec.slos.judgment.target")
        diff_incident = compute_rec_id("inc-B", "tighten_slo", "spec.slos.judgment.target")
        diff_type = compute_rec_id("inc-A", "add_deploy_gate", "spec.slos.judgment.target")
        diff_field = compute_rec_id("inc-A", "tighten_slo", "spec.slos.availability.target")

        assert base != diff_incident
        assert base != diff_type
        assert base != diff_field

    def test_recommendation_has_id_field(self):
        from nthlayer_workers.learn.recommendations import Recommendation

        rec = Recommendation(
            id="rec-deadbeef0123",
            service="fraud-detect",
            type="tighten_slo",
            rationale="test",
            proposed_value=98.5,
        )
        assert rec.id == "rec-deadbeef0123"
