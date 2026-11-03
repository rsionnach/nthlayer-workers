"""OTel instrumentation — records facts as events, never evaluates quality (ZFC).

Every event is an OTel log record emitted through nthlayer-common's
``emit_event`` (opensrm-quqo, A1 §4.0). They used to be span events on the
current span, but nothing in workers ever made a span recording, so all of
them were dropped. Each record's Timestamp is the event's own time (A1 T1):
the caller passes it where the event happened earlier than the emit.

Emitting is a no-op until the process calls ``init_telemetry`` with an OTLP
endpoint; every CLI entry point does.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from nthlayer_common.telemetry import emit_event

if TYPE_CHECKING:
    from nthlayer_workers.measure.detection.protocol import Alert
    from nthlayer_workers.measure.types import QualityScore


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

    Every record is stamped with ``score.timestamp``, when the evaluation was
    made.
    """
    # A1 §4.4: nthlayer.decision.id only when the decision is identifiable;
    # some adapters leave task_id empty.
    decision_attrs: dict[str, str] = (
        {"nthlayer.decision.id": score.task_id} if score.task_id else {}
    )
    if not score.auto_approved:
        for dimension, value in score.dimensions.items():
            emit_event(
                GRADER_EVENT,
                {
                    **decision_attrs,
                    "gen_ai.evaluation.name": dimension,
                    "gen_ai.evaluation.score.value": value,
                    "nthlayer.evaluator.kind": "implementation",
                    "gen_ai.agent.name": score.agent_name,
                    "nthlayer.evaluation.id": score.eval_id,
                    "nthlayer.evaluator.model": score.evaluator_model,
                    "nthlayer.evaluator.confidence": score.confidence,
                },
                timestamp=score.timestamp,
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
    emit_event("nthlayer.evaluation.completed", summary, timestamp=score.timestamp)


def emit_correction_event(
    eval_id: str,
    dimension: str,
    original: float,
    corrected: float,
    corrector: str,
    *,
    corrected_at: datetime,
    decision_id: str | None = None,
) -> None:
    """Record a human's corrected dimension score as a human grader evaluation.

    A1 §4.4: a human's verdict on a completion is a grader evaluation with
    ``nthlayer.evaluator.kind`` ``human``. ``decision_id`` is the evaluated
    task; it is omitted when unknown or empty. ``corrected_at`` is when the
    human made the correction, the record's Timestamp (A1 T1).
    """
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
    emit_event(GRADER_EVENT, attributes, timestamp=corrected_at)


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
    """Record a calibration/SLO report. It is computed as it is emitted, so
    its Timestamp is the emit time."""
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
    emit_event("nthlayer.calibration.report", attributes)


def emit_state_transition_event(
    agent_name: str,
    from_level: str,
    to_level: str,
    triggered_by: str,
    *,
    changed_at: datetime,
) -> None:
    """Record an autonomy state transition. ``changed_at`` is when the level
    changed, the record's Timestamp (A1 T1)."""
    emit_event(
        "nthlayer.autonomy.changed",
        {
            "agent_name": agent_name,
            "from_level": from_level,
            "to_level": to_level,
            "triggered_by": triggered_by,
        },
        timestamp=changed_at,
    )
