"""OTel instrumentation — records facts as events, never evaluates quality (ZFC).

Gracefully no-ops if opentelemetry is not installed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nthlayer_workers.measure.detection.protocol import Alert
    from nthlayer_workers.measure.types import QualityScore

try:
    from opentelemetry import trace

    _tracer = trace.get_tracer("nthlayer-measure")
    _HAS_OTEL = True
except ImportError:
    _tracer = None  # type: ignore[assignment]
    _HAS_OTEL = False


def emit_decision_event(score: QualityScore, alerts: list[Alert] | None = None) -> None:
    """Record an evaluation decision as a span event."""
    if not _HAS_OTEL:
        return
    span = trace.get_current_span()
    attributes: dict[str, str | float | int | bool] = {
        "eval_id": score.eval_id,
        "agent_name": score.agent_name,
        "task_id": score.task_id,
        "confidence": score.confidence,
        "evaluator_model": score.evaluator_model,
        "dimension_count": len(score.dimensions),
    }
    if score.cost_usd is not None:
        attributes["cost_usd"] = score.cost_usd
    if alerts:
        attributes["alert_count"] = len(alerts)
    span.add_event("gen_ai.decision.evaluated", attributes=attributes)


def emit_override_event(
    eval_id: str,
    dimension: str,
    original: float,
    corrected: float,
    corrector: str,
) -> None:
    """Record a human override as a span event."""
    if not _HAS_OTEL:
        return
    span = trace.get_current_span()
    span.add_event(
        "gen_ai.override.applied",
        attributes={
            "eval_id": eval_id,
            "dimension": dimension,
            "original_score": original,
            "corrected_score": corrected,
            "corrector": corrector,
        },
    )


def emit_calibration_report_event(
    agent_name: str,
    window_days: int,
    reversal_rate: float,
    false_accept_rate: float | None,
    precision: float | None,
    recall: float | None,
    mae: float,
    compliant: bool | None = None,
) -> None:
    """Record a calibration/SLO report as a span event."""
    if not _HAS_OTEL:
        return
    span = trace.get_current_span()
    attributes: dict[str, str | float | int | bool] = {
        "agent_name": agent_name,
        "window_days": window_days,
        "reversal_rate": reversal_rate,
        "mae": mae,
    }
    if false_accept_rate is not None:
        attributes["false_accept_rate"] = false_accept_rate
    if precision is not None:
        attributes["precision"] = precision
    if recall is not None:
        attributes["recall"] = recall
    if compliant is not None:
        attributes["reversal_rate_compliant"] = compliant
    span.add_event("gen_ai.calibration.report", attributes=attributes)


def emit_state_transition_event(
    agent_name: str,
    from_level: str,
    to_level: str,
    triggered_by: str,
) -> None:
    """Record an autonomy state transition as a span event."""
    if not _HAS_OTEL:
        return
    span = trace.get_current_span()
    span.add_event(
        "gen_ai.agent.state.changed",
        attributes={
            "agent_name": agent_name,
            "from_level": from_level,
            "to_level": to_level,
            "triggered_by": triggered_by,
        },
    )
