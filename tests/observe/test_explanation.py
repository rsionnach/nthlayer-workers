"""Tests for ExplanationEngine."""
from __future__ import annotations

from nthlayer_common.explanation import BudgetExplanation
from nthlayer_workers.observe.assessment import create as create_assessment
from nthlayer_workers.observe.explanation import ExplanationEngine
from nthlayer_workers.observe.store import MemoryAssessmentStore


def _make_slo_assessment(
    service: str = "svc",
    slo_name: str = "availability",
    percent_consumed: float = 12.0,
    status: str = "HEALTHY",
    burned_minutes: float = 100.0,
    total_budget_minutes: float = 1440.0,
    # observe/collector emits current_sli + objective in 0-100 percentage range
    # (collector multiplies sli_value by 100; YAML target uses the same
    # convention, e.g. availability target=99.9). Ratio fixtures (0.998/0.999)
    # were stale — they pre-dated the opensrm-ol4 ExplanationEngine fix that
    # repointed the formatter at the percentage convention. See opensrm-pa2w
    # for the cross-subsystem divergence (observe=percentage, measure=ratio).
    current_sli: float = 99.8,
    objective: float = 99.9,
):
    return create_assessment(
        kind="slo_status",
        service=service,
        data={
            "slo_name": slo_name,
            "objective": objective,
            "window": "30d",
            "total_budget_minutes": total_budget_minutes,
            "current_sli": current_sli,
            "burned_minutes": burned_minutes,
            "percent_consumed": percent_consumed,
            "status": status,
        },
    )


class TestExplanationEngine:
    def test_healthy_slo(self) -> None:
        store = MemoryAssessmentStore()
        store.put(_make_slo_assessment(status="HEALTHY", percent_consumed=12.0))
        results = ExplanationEngine().explain_service("svc", store)
        assert len(results) == 1
        assert results[0].severity == "info"
        assert "HEALTHY" in results[0].headline

    def test_warning_slo(self) -> None:
        store = MemoryAssessmentStore()
        store.put(_make_slo_assessment(status="WARNING", percent_consumed=73.0))
        results = ExplanationEngine().explain_service("svc", store)
        assert results[0].severity == "warning"
        assert "WARNING" in results[0].headline

    def test_critical_slo(self) -> None:
        store = MemoryAssessmentStore()
        store.put(_make_slo_assessment(status="CRITICAL", percent_consumed=92.0))
        results = ExplanationEngine().explain_service("svc", store)
        assert results[0].severity == "critical"

    def test_exhausted_slo(self) -> None:
        store = MemoryAssessmentStore()
        store.put(_make_slo_assessment(
            status="EXHAUSTED", percent_consumed=107.0,
            burned_minutes=1540, total_budget_minutes=1440,
        ))
        results = ExplanationEngine().explain_service("svc", store)
        assert results[0].severity == "critical"
        assert "exhausted" in results[0].headline.lower()

    def test_slo_filter(self) -> None:
        store = MemoryAssessmentStore()
        store.put(_make_slo_assessment(slo_name="availability"))
        store.put(_make_slo_assessment(slo_name="latency", status="WARNING", percent_consumed=55.0))
        results = ExplanationEngine().explain_service("svc", store, slo_filter="latency")
        assert len(results) == 1
        assert results[0].slo_name == "latency"

    def test_no_assessments(self) -> None:
        store = MemoryAssessmentStore()
        assert ExplanationEngine().explain_service("svc", store) == []

    def test_multiple_slos(self) -> None:
        store = MemoryAssessmentStore()
        store.put(_make_slo_assessment(slo_name="availability"))
        store.put(_make_slo_assessment(slo_name="latency"))
        results = ExplanationEngine().explain_service("svc", store)
        assert len(results) == 2

    def test_critical_has_actions(self) -> None:
        store = MemoryAssessmentStore()
        store.put(_make_slo_assessment(status="CRITICAL", percent_consumed=92.0))
        results = ExplanationEngine().explain_service("svc", store)
        assert len(results[0].recommended_actions) > 0

    def test_body_has_budget_math(self) -> None:
        store = MemoryAssessmentStore()
        store.put(_make_slo_assessment(
            burned_minutes=720, total_budget_minutes=1440, percent_consumed=50.0,
        ))
        results = ExplanationEngine().explain_service("svc", store)
        assert "720" in results[0].body
        assert "1440" in results[0].body

    def test_causes_when_over_80_percent(self) -> None:
        store = MemoryAssessmentStore()
        store.put(_make_slo_assessment(percent_consumed=85.0, status="CRITICAL"))
        results = ExplanationEngine().explain_service("svc", store)
        assert any("80%" in c for c in results[0].causes)

    def test_causes_when_sli_below_target(self) -> None:
        store = MemoryAssessmentStore()
        store.put(_make_slo_assessment(current_sli=99.5, objective=99.9))
        results = ExplanationEngine().explain_service("svc", store)
        assert any("below target" in c for c in results[0].causes)

    def test_returns_budget_explanation_type(self) -> None:
        store = MemoryAssessmentStore()
        store.put(_make_slo_assessment())
        results = ExplanationEngine().explain_service("svc", store)
        assert isinstance(results[0], BudgetExplanation)


# -- Drift-enriched causes (opensrm-pku) --

def _make_drift_assessment(
    service: str = "svc",
    slo_name: str = "availability",
    pattern: str = "stable",
    severity: str = "info",
    slope_per_week: float = 0.0,
    days_until_exhaustion: int | None = None,
):
    return create_assessment(
        kind="drift_signal",
        service=service,
        data={
            "slo_name": slo_name,
            "severity": severity,
            "pattern": pattern,
            "slope_per_week": slope_per_week,
            "days_until_exhaustion": days_until_exhaustion,
            "current_budget": 0.5,
            "summary": "stub",
            "recommendation": "stub",
        },
    )


class TestExplanationDriftCauses:
    """ExplanationEngine surfaces drift-pattern context in causes (opensrm-pku)."""

    def test_no_drift_signal_preserves_existing_causes(self) -> None:
        store = MemoryAssessmentStore()
        store.put(_make_slo_assessment(percent_consumed=85.0, status="CRITICAL"))
        results = ExplanationEngine().explain_service("svc", store)
        # Existing 80%-consumption cause still present; nothing added.
        assert any("80%" in c for c in results[0].causes)
        assert not any("drift" in c.lower() for c in results[0].causes)

    def test_gradual_decline_adds_cause(self) -> None:
        store = MemoryAssessmentStore()
        store.put(_make_slo_assessment(slo_name="availability"))
        store.put(_make_drift_assessment(
            slo_name="availability",
            pattern="gradual_decline",
            slope_per_week=-0.0042,
        ))
        results = ExplanationEngine().explain_service("svc", store)
        assert any("declining" in c.lower() and "%/week" in c for c in results[0].causes)

    def test_step_change_down_adds_cause(self) -> None:
        store = MemoryAssessmentStore()
        store.put(_make_slo_assessment(slo_name="availability"))
        store.put(_make_drift_assessment(
            slo_name="availability",
            pattern="step_change_down",
            slope_per_week=-0.012,
        ))
        results = ExplanationEngine().explain_service("svc", store)
        assert any("step-change" in c.lower() and "deploy" in c.lower()
                   for c in results[0].causes)

    def test_projected_exhaustion_appended_when_set(self) -> None:
        store = MemoryAssessmentStore()
        store.put(_make_slo_assessment(slo_name="availability"))
        store.put(_make_drift_assessment(
            slo_name="availability",
            pattern="gradual_decline",
            slope_per_week=-0.005,
            days_until_exhaustion=21,
        ))
        results = ExplanationEngine().explain_service("svc", store)
        assert any("Projected exhaustion in 21 days" in c for c in results[0].causes)

    def test_projected_exhaustion_skipped_when_none(self) -> None:
        store = MemoryAssessmentStore()
        store.put(_make_slo_assessment(slo_name="availability"))
        store.put(_make_drift_assessment(
            slo_name="availability",
            pattern="gradual_decline",
            slope_per_week=-0.003,
            days_until_exhaustion=None,
        ))
        results = ExplanationEngine().explain_service("svc", store)
        assert not any("Projected exhaustion" in c for c in results[0].causes)

    def test_stable_pattern_adds_no_drift_cause(self) -> None:
        store = MemoryAssessmentStore()
        store.put(_make_slo_assessment(slo_name="availability"))
        store.put(_make_drift_assessment(
            slo_name="availability", pattern="stable", slope_per_week=0.0,
        ))
        results = ExplanationEngine().explain_service("svc", store)
        assert not any("drift" in c.lower() or "step-change" in c.lower()
                       or "Projected exhaustion" in c for c in results[0].causes)

    def test_volatile_pattern_adds_cause(self) -> None:
        store = MemoryAssessmentStore()
        store.put(_make_slo_assessment(slo_name="availability"))
        store.put(_make_drift_assessment(
            slo_name="availability", pattern="volatile", slope_per_week=0.0,
        ))
        results = ExplanationEngine().explain_service("svc", store)
        assert any("variance" in c.lower() or "unstable" in c.lower()
                   for c in results[0].causes)

    def test_drift_matched_per_slo_name(self) -> None:
        """Drift for SLO A must not enrich SLO B's explanation."""
        store = MemoryAssessmentStore()
        store.put(_make_slo_assessment(slo_name="availability"))
        store.put(_make_slo_assessment(slo_name="latency"))
        store.put(_make_drift_assessment(
            slo_name="availability",
            pattern="step_change_down",
            slope_per_week=-0.01,
        ))
        results = ExplanationEngine().explain_service("svc", store)
        avail = next(r for r in results if r.slo_name == "availability")
        lat = next(r for r in results if r.slo_name == "latency")
        assert any("step-change" in c.lower() for c in avail.causes)
        assert not any("step-change" in c.lower() for c in lat.causes)

    def test_drift_query_bounded_to_service(self) -> None:
        """Drift signals from another service must not bleed into svc's causes."""
        store = MemoryAssessmentStore()
        store.put(_make_slo_assessment(service="svc", slo_name="availability"))
        store.put(_make_drift_assessment(
            service="other", slo_name="availability",
            pattern="step_change_down", slope_per_week=-0.05,
        ))
        results = ExplanationEngine().explain_service("svc", store)
        assert not any("step-change" in c.lower() for c in results[0].causes)

    def test_latest_drift_wins_per_slo(self) -> None:
        """Multiple drift_signal assessments for the same SLO use the most recent."""
        store = MemoryAssessmentStore()
        store.put(_make_slo_assessment(slo_name="availability"))
        # Older first, newer second — store iteration is desc, so newer first.
        store.put(_make_drift_assessment(
            slo_name="availability", pattern="gradual_decline", slope_per_week=-0.001,
        ))
        store.put(_make_drift_assessment(
            slo_name="availability", pattern="step_change_down", slope_per_week=-0.05,
        ))
        results = ExplanationEngine().explain_service("svc", store)
        # Newer (step_change_down) wins; gradual_decline cause should not appear.
        assert any("step-change" in c.lower() for c in results[0].causes)
        assert not any("gradually declining" in c.lower() for c in results[0].causes)
