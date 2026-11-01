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


# OpenSRM Addendum A1 (opensrm-ju8r). Measure's scores are grader evaluations,
# emitted as the upstream grader event (A1 §4.4). The other events here are
# nthlayer.* implementation events: A1 does not define them, and A1 §4.0 bars
# gen_ai.* names for anything OpenSRM-defined or our own. The calibration and
# autonomy events only had their names changed: their attributes stay bare
# (agent_name, from_level), unlike the namespaced grader attributes.
GRADER_EVENT = "gen_ai.evaluation.result"


def emit_evaluation_events(score: QualityScore, alerts: list[Alert] | None = None) -> None:
    """Record an evaluation: one grader event per dimension, plus a summary.

    Each dimension score is a ``gen_ai.evaluation.result`` about the evaluated
    task's decision, with ``nthlayer.evaluator.kind`` ``implementation``
    (NthLayer's evaluator, vs ``human`` in ``emit_correction_event``). Per-evaluation facts
    (cost, alerts, dimension count) go on one ``nthlayer.evaluation.completed``
    event instead, so they are not repeated, and summed, once per dimension.

    An auto-approved output emits the summary only: no evaluator ran, so its
    configured scores are nobody's opinion and must not reach grader
    consumers (A1 §3).
    """
    if not _HAS_OTEL:
        return
    span = trace.get_current_span()
    # A1 §4.4: nthlayer.decision.id only when the decision is identifiable;
    # some adapters leave task_id empty.
    decision_attrs: dict[str, str] = (
        {"nthlayer.decision.id": score.task_id} if score.task_id else {}
    )
    if not score.auto_approved:
        for dimension, value in score.dimensions.items():
            span.add_event(
                GRADER_EVENT,
                attributes={
                    **decision_attrs,
                    "gen_ai.evaluation.name": dimension,
                    "gen_ai.evaluation.score.value": value,
                    "nthlayer.evaluator.kind": "implementation",
                    "gen_ai.agent.name": score.agent_name,
                    "nthlayer.evaluation.id": score.eval_id,
                    "nthlayer.evaluator.model": score.evaluator_model,
                    "nthlayer.evaluator.confidence": score.confidence,
                },
            )
    summary: dict[str, str | float | int | bool] = {
        **decision_attrs,
        "nthlayer.evaluation.id": score.eval_id,
        "gen_ai.agent.name": score.agent_name,
        "nthlayer.evaluation.dimension_count": len(score.dimensions),
        "nthlayer.evaluation.auto_approved": score.auto_approved,
    }
    if score.cost_usd is not None:
        summary["nthlayer.evaluation.cost_usd"] = score.cost_usd
    if alerts:
        summary["nthlayer.evaluation.alert_count"] = len(alerts)
    span.add_event("nthlayer.evaluation.completed", attributes=summary)


def emit_correction_event(
    eval_id: str,
    dimension: str,
    original: float,
    corrected: float,
    corrector: str,
    *,
    decision_id: str | None = None,
) -> None:
    """Record a human's corrected dimension score as a human grader evaluation.

    A1 §4.4: a human's verdict on a completion is a grader evaluation with
    ``nthlayer.evaluator.kind`` ``human``. ``decision_id`` is the evaluated
    task; it is omitted when unknown or empty.
    """
    if not _HAS_OTEL:
        return
    span = trace.get_current_span()
    attributes: dict[str, str | float | int | bool] = {
        "gen_ai.evaluation.name": dimension,
        "gen_ai.evaluation.score.value": corrected,
        "nthlayer.evaluator.kind": "human",
        "nthlayer.evaluation.id": eval_id,
        "nthlayer.evaluation.original_score": original,
        "nthlayer.evaluator.actor": corrector,
    }
    if decision_id:
        attributes["nthlayer.decision.id"] = decision_id
    span.add_event(GRADER_EVENT, attributes=attributes)


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
    span.add_event("nthlayer.calibration.report", attributes=attributes)


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
        "nthlayer.autonomy.changed",
        attributes={
            "agent_name": agent_name,
            "from_level": from_level,
            "to_level": to_level,
            "triggered_by": triggered_by,
        },
    )
