"""Tests for the Learn → Spec recommendation engine (opensrm-jmy.2)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
import yaml

from nthlayer_common.outcomes import FinancialImpact

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
        assert out["apiVersion"] == "nthlayer.io/learn/v1"
        assert out["kind"] == "RecommendationPlan"
        assert out["metadata"]["incident"] == "INC-X"
        assert out["metadata"]["confidence"] == 0.8
        assert out["metadata"]["requires_human_review"] is True
        assert len(out["recommendations"]) == 1
        assert out["recommendations"][0]["type"] == "tighten_slo"

    def test_to_yaml_drops_empty_optional_fields(self):
        """Empty optional fields stay out of the YAML so it matches the spec example.

        opensrm-jmy.23: financial_impact moved off Recommendation to
        SpecRecommendation.metadata, so the per-rec dict should not carry it
        regardless of population state — it's no longer a Recommendation field.
        """
        s = SpecRecommendation(
            incident="INC-Y", generated_by="nthlayer-learn",
            generated_at=datetime.now(timezone.utc),
            confidence=0.5,
            recommendations=[
                Recommendation(
                    id="rec-test00000002",
                    service="svc", type="tighten_slo",
                    rationale="x", proposed_value=99.95,
                    # field, current_value, evidence omitted
                ),
            ],
        )
        out = yaml.safe_load(s.to_yaml())
        rec = out["recommendations"][0]
        # financial_impact is not a Recommendation field at all (jmy.23).
        assert "financial_impact" not in rec
        assert "evidence" not in rec

    def test_to_yaml_metadata_includes_financial_impact_when_populated(self):
        """jmy.23: SpecRecommendation.financial_impact lands in top-level metadata."""
        fi = FinancialImpact(
            estimated=5400.0,
            currency="USD",
            decisions_affected=1200,
            failure_mode="false_negative",
            volume_source="metric",
        )
        s = SpecRecommendation(
            incident="INC-FI", generated_by="nthlayer-learn",
            generated_at=datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc),
            confidence=0.8,
            recommendations=[
                Recommendation(
                    id="rec-test00000003",
                    service="svc-a", type="tighten_slo",
                    rationale="x", proposed_value=99.95,
                ),
            ],
            financial_impact=fi,
        )
        out = yaml.safe_load(s.to_yaml())
        meta_fi = out["metadata"]["financial_impact"]
        assert meta_fi["estimated"] == 5400.0
        assert meta_fi["currency"] == "USD"
        assert meta_fi["decisions_affected"] == 1200
        assert meta_fi["failure_mode"] == "false_negative"
        assert meta_fi["volume_source"] == "metric"

    def test_to_yaml_metadata_omits_financial_impact_when_none(self):
        """jmy.23: absent FinancialImpact → metadata.financial_impact key absent."""
        s = SpecRecommendation(
            incident="INC-NOFI", generated_by="nthlayer-learn",
            generated_at=datetime.now(timezone.utc),
            confidence=0.5,
            recommendations=[
                Recommendation(
                    id="rec-test00000004",
                    service="svc", type="tighten_slo",
                    rationale="x", proposed_value=99.95,
                ),
            ],
        )
        out = yaml.safe_load(s.to_yaml())
        assert "financial_impact" not in out["metadata"]

    def test_to_yaml_with_zero_estimated_keeps_field(self):
        """jmy.23: FinancialImpact with estimated=0.0 must survive emission.

        The per-Recommendation filter at _to_dict drops `(None, [], "")` from
        recommendation dicts. metadata is built separately and not subject to
        that filter — verify a $0/0-decisions figure still emits in full.
        """
        fi = FinancialImpact(
            estimated=0.0, currency="USD", decisions_affected=0,
            failure_mode="false_negative", volume_source="metric",
        )
        s = SpecRecommendation(
            incident="INC-ZERO", generated_by="nthlayer-learn",
            generated_at=datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc),
            confidence=0.5,
            recommendations=[
                Recommendation(
                    id="rec-test00000099",
                    service="svc", type="tighten_slo",
                    rationale="x", proposed_value=99.95,
                ),
            ],
            financial_impact=fi,
        )
        out = yaml.safe_load(s.to_yaml())
        assert "financial_impact" in out["metadata"]
        assert out["metadata"]["financial_impact"]["estimated"] == 0.0
        assert out["metadata"]["financial_impact"]["decisions_affected"] == 0

    def test_yaml_round_trip_preserves_financial_impact(self):
        """jmy.23: every FinancialImpact subfield round-trips through YAML with correct type."""
        fi = FinancialImpact(
            estimated=5400.0,
            currency="USD",
            decisions_affected=1200,
            failure_mode="false_negative",
            volume_source="metric",
        )
        s = SpecRecommendation(
            incident="INC-RT-FI", generated_by="nthlayer-learn",
            generated_at=datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc),
            confidence=0.7,
            recommendations=[
                Recommendation(
                    id="rec-test00000005",
                    service="svc-a", type="tighten_slo",
                    rationale="x", proposed_value=99.95,
                ),
            ],
            financial_impact=fi,
        )
        loaded = yaml.safe_load(s.to_yaml())
        meta_fi = loaded["metadata"]["financial_impact"]
        assert isinstance(meta_fi["estimated"], float)
        assert meta_fi["estimated"] == 5400.0
        assert isinstance(meta_fi["decisions_affected"], int)
        assert meta_fi["decisions_affected"] == 1200
        assert isinstance(meta_fi["currency"], str)
        assert meta_fi["currency"] == "USD"
        assert isinstance(meta_fi["failure_mode"], str)
        assert meta_fi["failure_mode"] == "false_negative"
        assert isinstance(meta_fi["volume_source"], str)
        assert meta_fi["volume_source"] == "metric"


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
# jmy.23: analyze_incident propagates retrospective financial_impact
# ---------------------------------------------------------------------------


class TestAnalyzeIncidentFinancialImpact:
    """jmy.23: analyze_incident reads retrospective_data["financial_impact"]
    and attaches a FinancialImpact instance to the returned SpecRecommendation."""

    def test_analyze_incident_propagates_financial_impact_when_present(self):
        retro = _retrospective_with_breach(target=98.5, current=92.0)
        retro["financial_impact"] = {
            "estimated": 5400.0,
            "currency": "USD",
            "decisions_affected": 1200,
            "failure_mode": "false_negative",
            "volume_source": "metric",
        }
        result = analyze_incident(retro, "inc-test-1")
        assert isinstance(result.financial_impact, FinancialImpact)
        assert result.financial_impact.estimated == 5400.0
        assert result.financial_impact.currency == "USD"
        assert result.financial_impact.decisions_affected == 1200
        assert result.financial_impact.failure_mode == "false_negative"
        assert result.financial_impact.volume_source == "metric"

    def test_analyze_incident_omits_financial_impact_when_absent(self):
        retro = _retrospective_with_breach(target=98.5, current=92.0)
        # No "financial_impact" key at all.
        result = analyze_incident(retro, "inc-test-2")
        assert result.financial_impact is None

    def test_analyze_incident_omits_financial_impact_when_null(self):
        retro = _retrospective_with_breach(target=98.5, current=92.0)
        retro["financial_impact"] = None  # retrospective computed None
        result = analyze_incident(retro, "inc-test-3")
        assert result.financial_impact is None

    @pytest.mark.parametrize("bad", ["not a dict", 123, [1, 2, 3]])
    def test_analyze_incident_raises_on_wrong_type_financial_impact(self, bad):
        retro = _retrospective_with_breach(target=98.5, current=92.0)
        retro["financial_impact"] = bad
        with pytest.raises(TypeError, match="must be a mapping"):
            analyze_incident(retro, "inc-bad-type")

    def test_analyze_incident_wrong_type_error_names_incident(self):
        retro = _retrospective_with_breach(target=98.5, current=92.0)
        retro["financial_impact"] = "not a dict"
        with pytest.raises(TypeError, match="inc-named-test"):
            analyze_incident(retro, "inc-named-test")

    def test_analyze_incident_raises_on_missing_required_keys(self):
        retro = _retrospective_with_breach(target=98.5, current=92.0)
        retro["financial_impact"] = {"estimated": 1.0, "currency": "USD"}
        with pytest.raises(TypeError):
            analyze_incident(retro, "inc-missing-keys")

    def test_analyze_incident_raises_on_extra_keys(self):
        retro = _retrospective_with_breach(target=98.5, current=92.0)
        retro["financial_impact"] = {
            "estimated": 1.0, "currency": "USD",
            "decisions_affected": 1, "failure_mode": "false_negative",
            "volume_source": "metric", "bogus": "x",
        }
        with pytest.raises(TypeError):
            analyze_incident(retro, "inc-extra-keys")

    def test_analyze_incident_treats_empty_dict_as_none(self):
        retro = _retrospective_with_breach(target=98.5, current=92.0)
        retro["financial_impact"] = {}
        result = analyze_incident(retro, "inc-empty-dict")
        assert result.financial_impact is None

    def test_analyze_incident_does_not_mutate_retrospective(self):
        import copy
        retro = _retrospective_with_breach(target=98.5, current=92.0)
        retro["financial_impact"] = {
            "estimated": 5400.0, "currency": "USD",
            "decisions_affected": 1200, "failure_mode": "false_negative",
            "volume_source": "metric",
        }
        snapshot = copy.deepcopy(retro)
        analyze_incident(retro, "inc-no-mutate")
        assert retro == snapshot


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


# ---------------------------------------------------------------------------
# jmy.6: apiVersion + kind rename for the plan-file artefact
# ---------------------------------------------------------------------------


class TestPlanArtefactRename:
    """jmy.6: apiVersion + kind rename for the plan-file artefact."""

    def test_to_yaml_emits_new_api_version(self):
        from nthlayer_workers.learn.recommendations import SpecRecommendation, Recommendation
        from datetime import datetime, timezone

        sr = SpecRecommendation(
            incident="inc-test",
            generated_by="nthlayer-learn",
            generated_at=datetime(2026, 5, 26, tzinfo=timezone.utc),
            confidence=0.7,
            recommendations=[
                Recommendation(
                    id="rec-deadbeef0123",
                    service="fraud-detect",
                    type="tighten_slo",
                    rationale="test",
                    proposed_value=98.5,
                ),
            ],
        )

        yaml_text = sr.to_yaml()
        assert "apiVersion: nthlayer.io/learn/v1" in yaml_text
        assert "kind: RecommendationPlan" in yaml_text
        # Old strings absent
        assert "opensrm.io/v1" not in yaml_text
        assert "SpecRecommendation" not in yaml_text


# ---------------------------------------------------------------------------
# jmy.6: OutcomeKind enum
# ---------------------------------------------------------------------------


class TestOutcomeKind:
    """jmy.6: OutcomeKind enum lives in recommendations.py (operator-visible)."""

    def test_outcome_kind_values(self):
        from nthlayer_workers.learn.recommendations import OutcomeKind

        assert OutcomeKind.APPLY_CLEAN.value == "apply_clean"
        assert OutcomeKind.ALREADY_APPLIED.value == "already_applied"
        assert OutcomeKind.DRIFT_DETECTED.value == "drift_detected"
        assert OutcomeKind.TARGET_PATH_MISSING.value == "target_path_missing"
        assert OutcomeKind.MANIFEST_NOT_FOUND.value == "manifest_not_found"

    def test_outcome_kind_is_string_enum(self):
        """StrEnum gives wire-safe string serialisation for free."""
        from nthlayer_workers.learn.recommendations import OutcomeKind

        assert OutcomeKind.APPLY_CLEAN == "apply_clean"
        assert str(OutcomeKind.DRIFT_DETECTED) == "drift_detected"


# ---------------------------------------------------------------------------
# jmy.6: --from <plan.yaml> validation
# ---------------------------------------------------------------------------


class TestParsePlanFile:
    """jmy.6: --from <plan.yaml> validation."""

    def test_parse_plan_file_happy_path(self, tmp_path):
        from nthlayer_workers.learn.recommendations import (
            SpecRecommendation, parse_plan_file,
        )
        from datetime import datetime, timezone

        original = SpecRecommendation(
            incident="inc-test",
            generated_by="nthlayer-learn",
            generated_at=datetime(2026, 5, 26, tzinfo=timezone.utc),
            confidence=0.7,
            recommendations=[],
        )
        plan_path = tmp_path / "plan.yaml"
        plan_path.write_text(original.to_yaml())

        loaded = parse_plan_file(plan_path)
        assert loaded.incident == "inc-test"
        assert loaded.confidence == 0.7
        assert loaded.requires_human_review is True

    def test_parse_plan_file_unknown_api_version_raises(self, tmp_path):
        from nthlayer_workers.learn.recommendations import (
            parse_plan_file, PlanFileUnknownVersionError,
        )

        plan_path = tmp_path / "plan.yaml"
        plan_path.write_text(
            "apiVersion: opensrm.io/v999\n"
            "kind: RecommendationPlan\n"
            "metadata: {incident: x, generated_by: y, generated_at: '2026-01-01T00:00:00+00:00', confidence: 0.5, requires_human_review: true}\n"
            "recommendations: []\n"
        )

        with pytest.raises(PlanFileUnknownVersionError, match="nthlayer.io/learn/v1"):
            parse_plan_file(plan_path)

    def test_parse_plan_file_missing_recommendations_key_raises(self, tmp_path):
        from nthlayer_workers.learn.recommendations import (
            parse_plan_file, PlanFileInvalidError,
        )

        plan_path = tmp_path / "plan.yaml"
        plan_path.write_text(
            "apiVersion: nthlayer.io/learn/v1\n"
            "kind: RecommendationPlan\n"
            "metadata: {incident: x, generated_by: y, generated_at: '2026-01-01T00:00:00+00:00', confidence: 0.5, requires_human_review: true}\n"
        )

        with pytest.raises(PlanFileInvalidError, match="recommendations"):
            parse_plan_file(plan_path)

    def test_parse_plan_file_non_list_recommendations_raises(self, tmp_path):
        from nthlayer_workers.learn.recommendations import (
            parse_plan_file, PlanFileInvalidError,
        )

        plan_path = tmp_path / "plan.yaml"
        plan_path.write_text(
            "apiVersion: nthlayer.io/learn/v1\n"
            "kind: RecommendationPlan\n"
            "metadata: {incident: x, generated_by: y, generated_at: '2026-01-01T00:00:00+00:00', confidence: 0.5, requires_human_review: true}\n"
            "recommendations: not_a_list\n"
        )

        with pytest.raises(PlanFileInvalidError, match="recommendations must be a list"):
            parse_plan_file(plan_path)

    def test_parse_plan_file_coerces_naive_datetime_to_utc(self, tmp_path):
        """jmy.6 P1 R5: naive generated_at coerced to UTC to prevent comparison bugs."""
        from nthlayer_workers.learn.recommendations import parse_plan_file
        from datetime import timezone

        plan_path = tmp_path / "plan.yaml"
        plan_path.write_text(
            "apiVersion: nthlayer.io/learn/v1\n"
            "kind: RecommendationPlan\n"
            "metadata: {incident: x, generated_by: y, generated_at: '2026-05-01T10:00:00', confidence: 0.5, requires_human_review: true}\n"
            "recommendations: []\n"
        )

        loaded = parse_plan_file(plan_path)
        assert loaded.generated_at.tzinfo is timezone.utc

    # ---- jmy.23: financial_impact round-trip through parse_plan_file ----

    def test_parse_plan_file_round_trips_financial_impact(self, tmp_path):
        """jmy.23: to_yaml() → parse_plan_file preserves the FinancialImpact figure."""
        from nthlayer_workers.learn.recommendations import parse_plan_file

        fi = FinancialImpact(
            estimated=5400.0, currency="USD", decisions_affected=1200,
            failure_mode="false_negative", volume_source="metric",
        )
        original = SpecRecommendation(
            incident="inc-rt", generated_by="nthlayer-learn",
            generated_at=datetime(2026, 5, 28, tzinfo=timezone.utc),
            confidence=0.7, recommendations=[], financial_impact=fi,
        )
        plan_path = tmp_path / "plan.yaml"
        plan_path.write_text(original.to_yaml())

        loaded = parse_plan_file(plan_path)
        assert loaded.financial_impact == fi

    def test_parse_plan_file_legacy_yaml_without_financial_impact(self, tmp_path):
        """jmy.23: pre-jmy.23 plan files (no metadata.financial_impact) load with None."""
        from nthlayer_workers.learn.recommendations import parse_plan_file

        plan_path = tmp_path / "plan.yaml"
        plan_path.write_text(
            "apiVersion: nthlayer.io/learn/v1\n"
            "kind: RecommendationPlan\n"
            "metadata: {incident: x, generated_by: y, generated_at: '2026-01-01T00:00:00+00:00', confidence: 0.5, requires_human_review: true}\n"
            "recommendations: []\n"
        )
        loaded = parse_plan_file(plan_path)
        assert loaded.financial_impact is None

    def test_parse_plan_file_rejects_malformed_financial_impact_wrong_type(self, tmp_path):
        """jmy.23: non-dict financial_impact raises PlanFileInvalidError."""
        from nthlayer_workers.learn.recommendations import (
            parse_plan_file, PlanFileInvalidError,
        )

        plan_path = tmp_path / "plan.yaml"
        plan_path.write_text(
            "apiVersion: nthlayer.io/learn/v1\n"
            "kind: RecommendationPlan\n"
            "metadata:\n"
            "  incident: x\n"
            "  generated_by: y\n"
            "  generated_at: '2026-01-01T00:00:00+00:00'\n"
            "  confidence: 0.5\n"
            "  requires_human_review: true\n"
            "  financial_impact: not-a-dict\n"
            "recommendations: []\n"
        )
        with pytest.raises(PlanFileInvalidError, match="financial_impact must be a mapping"):
            parse_plan_file(plan_path)

    def test_parse_plan_file_rejects_malformed_financial_impact_missing_keys(self, tmp_path):
        """jmy.23: dict missing required FinancialImpact keys → PlanFileInvalidError."""
        from nthlayer_workers.learn.recommendations import (
            parse_plan_file, PlanFileInvalidError,
        )

        plan_path = tmp_path / "plan.yaml"
        plan_path.write_text(
            "apiVersion: nthlayer.io/learn/v1\n"
            "kind: RecommendationPlan\n"
            "metadata:\n"
            "  incident: x\n"
            "  generated_by: y\n"
            "  generated_at: '2026-01-01T00:00:00+00:00'\n"
            "  confidence: 0.5\n"
            "  requires_human_review: true\n"
            "  financial_impact: {estimated: 1.0}\n"
            "recommendations: []\n"
        )
        with pytest.raises(PlanFileInvalidError, match="financial_impact invalid"):
            parse_plan_file(plan_path)
