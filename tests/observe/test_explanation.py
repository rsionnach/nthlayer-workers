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
