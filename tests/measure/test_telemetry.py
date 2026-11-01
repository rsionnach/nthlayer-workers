"""Tests for OTel telemetry instrumentation.

Event shapes follow OpenSRM Addendum A1 (opensrm-ju8r): measure's scores are
grader evaluations, emitted as the upstream ``gen_ai.evaluation.result``
(A1 §4.4); the rest are ``nthlayer.*`` implementation events. No ``gen_ai.``
form of an OpenSRM-defined event is emitted (A1 §4.0).
"""

from unittest.mock import MagicMock, patch

from nthlayer_workers.measure.detection.protocol import Alert
from nthlayer_workers.measure.types import QualityScore

GRADER_EVENT = "gen_ai.evaluation.result"
RETIRED = (
    "gen_ai.decision.evaluated",
    "gen_ai.override.applied",
    "gen_ai.calibration.report",
    "gen_ai.agent.state.changed",
)


def _make_score(**kwargs) -> QualityScore:
    defaults = dict(
        eval_id="e1",
        agent_name="agent-a",
        task_id="t1",
        dimensions={"correctness": 0.9, "safety": 0.7},
        confidence=0.85,
        evaluator_model="test-model",
        cost_usd=0.01,
    )
    defaults.update(kwargs)
    return QualityScore(**defaults)


def _events(emit) -> list[tuple[str, dict]]:
    """Run *emit* against a mocked current span; return (name, attributes) pairs."""
    mock_span = MagicMock()
    with patch("nthlayer_workers.measure.telemetry._HAS_OTEL", True), \
         patch("nthlayer_workers.measure.telemetry.trace") as mock_trace:
        mock_trace.get_current_span.return_value = mock_span
        emit()
    events = [(c.args[0], c.kwargs["attributes"]) for c in mock_span.add_event.call_args_list]
    assert not [name for name, _ in events if name in RETIRED]
    return events


def test_evaluation_is_one_grader_event_per_dimension():
    from nthlayer_workers.measure.telemetry import emit_decision_event

    events = _events(lambda: emit_decision_event(_make_score()))

    graded = {a["gen_ai.evaluation.name"]: a for n, a in events if n == GRADER_EVENT}
    assert set(graded) == {"correctness", "safety"}
    assert graded["correctness"]["gen_ai.evaluation.score.value"] == 0.9
    assert graded["safety"]["gen_ai.evaluation.score.value"] == 0.7
    for attrs in graded.values():
        # A1 §4.4: the evaluator kind, and the decision the evaluation is about.
        assert attrs["nthlayer.evaluator.kind"] == "implementation"
        assert attrs["nthlayer.decision.id"] == "t1"
        assert attrs["gen_ai.agent.name"] == "agent-a"
        assert attrs["nthlayer.evaluation.id"] == "e1"
        assert attrs["nthlayer.evaluator.model"] == "test-model"
        assert attrs["nthlayer.evaluator.confidence"] == 0.85
        # Per-evaluation facts stay off per-dimension events: summing cost
        # over them would count it once per dimension.
        assert "nthlayer.evaluation.cost_usd" not in attrs


def test_evaluation_summary_carries_per_evaluation_facts():
    from nthlayer_workers.measure.telemetry import emit_decision_event

    alerts = [
        Alert(agent_name="agent-a", metric_name="reversal_rate",
              current_value=0.5, threshold=0.3, message="too high")
    ]
    events = _events(lambda: emit_decision_event(_make_score(), alerts))

    (summary,) = [a for n, a in events if n == "nthlayer.evaluation.completed"]
    assert summary["nthlayer.evaluation.id"] == "e1"
    assert summary["nthlayer.decision.id"] == "t1"
    assert summary["nthlayer.evaluation.dimension_count"] == 2
    assert summary["nthlayer.evaluation.cost_usd"] == 0.01
    assert summary["nthlayer.evaluation.alert_count"] == 1
    assert summary["nthlayer.evaluation.auto_approved"] is False


def test_auto_approved_output_is_not_a_grader_evaluation():
    """No evaluator ran, so the configured scores are not anyone's opinion."""
    from nthlayer_workers.measure.telemetry import emit_decision_event

    score = _make_score(auto_approved=True, evaluator_model="auto-approved", cost_usd=None)
    events = _events(lambda: emit_decision_event(score))

    assert [n for n, _ in events] == ["nthlayer.evaluation.completed"]
    summary = events[0][1]
    assert summary["nthlayer.evaluation.auto_approved"] is True
    assert "nthlayer.evaluation.cost_usd" not in summary
    assert "nthlayer.evaluation.alert_count" not in summary


def test_noop_without_otel():
    with patch("nthlayer_workers.measure.telemetry._HAS_OTEL", False):
        from nthlayer_workers.measure.telemetry import emit_decision_event
        # Should not raise even with no OTel
        emit_decision_event(_make_score())


def test_human_correction_is_a_human_grader_evaluation():
    """A1 §4.4: a human's verdict on a completion is a grader evaluation."""
    from nthlayer_workers.measure.telemetry import emit_override_event

    events = _events(lambda: emit_override_event(
        "e1", "correctness", 0.9, 0.5, "reviewer", decision_id="t1",
    ))

    (attrs,) = [a for n, a in events if n == GRADER_EVENT]
    assert len(events) == 1
    assert attrs["gen_ai.evaluation.name"] == "correctness"
    assert attrs["gen_ai.evaluation.score.value"] == 0.5
    assert attrs["nthlayer.evaluator.kind"] == "human"
    assert attrs["nthlayer.decision.id"] == "t1"
    assert attrs["nthlayer.evaluation.id"] == "e1"
    assert attrs["nthlayer.evaluation.original_score"] == 0.9
    assert attrs["nthlayer.evaluator.actor"] == "reviewer"


def test_calibration_report_is_an_implementation_event():
    from nthlayer_workers.measure.telemetry import emit_calibration_report_event

    events = _events(lambda: emit_calibration_report_event(
        agent_name="agent-a", window_days=30, reversal_rate=0.1,
        false_accept_rate=None, precision=0.8, recall=None, mae=0.05,
    ))

    assert [n for n, _ in events] == ["nthlayer.calibration.report"]
    attrs = events[0][1]
    assert attrs["agent_name"] == "agent-a"
    assert attrs["reversal_rate"] == 0.1
    assert attrs["precision"] == 0.8
    assert "recall" not in attrs


def test_autonomy_change_is_an_implementation_event():
    from nthlayer_workers.measure.telemetry import emit_state_transition_event

    events = _events(lambda: emit_state_transition_event(
        "agent-a", "full", "supervised", "governance",
    ))

    assert [n for n, _ in events] == ["nthlayer.autonomy.changed"]
    attrs = events[0][1]
    assert attrs["from_level"] == "full"
    assert attrs["to_level"] == "supervised"
