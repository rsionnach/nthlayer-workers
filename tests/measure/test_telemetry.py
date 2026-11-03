"""Tests for OTel telemetry instrumentation.

Event shapes follow OpenSRM Addendum A1 (opensrm-ju8r): measure's scores are
grader evaluations, emitted as the upstream ``gen_ai.evaluation.result``
(A1 §4.4); the rest are ``nthlayer.*`` implementation events. No ``gen_ai.``
form of an OpenSRM-defined event is emitted (A1 §4.0).

Every event is an OTel log record (opensrm-quqo), asserted on through
nthlayer-common's ``capture_events()``, whose Timestamp is the event's own
time (A1 T1).
"""

from datetime import UTC, datetime

from nthlayer_common.telemetry import capture_events
from opentelemetry._logs import SeverityNumber

from nthlayer_workers.measure.detection.protocol import Alert
from nthlayer_workers.measure.types import QualityScore

GRADER_EVENT = "gen_ai.evaluation.result"
RETIRED_EVENT_NAMES = (
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


# An event time well clear of "now", so a record stamped with the emit time
# instead of the event's own fails the timestamp assertions.
EVENT_TIME = datetime(2026, 1, 2, 3, 4, 5, 678000, tzinfo=UTC)
EVENT_TIME_NS = 1767323045678000000
INFO = SeverityNumber.INFO.value  # ruling Q2


def _records(emit):
    """Run *emit*; return the log records it emitted, asserting each is an
    INFO record and that no retired event name was emitted."""
    with capture_events() as cap:
        emit()
    records = cap.events
    assert not [r.name for r in records if r.name in RETIRED_EVENT_NAMES]
    assert {r.severity_number for r in records} <= {INFO}
    return records


def _events(emit) -> list[tuple[str, dict]]:
    """(name, attributes) for each record *emit* emitted."""
    return [(r.name, r.attributes) for r in _records(emit)]


def test_evaluation_is_one_grader_event_per_dimension():
    from nthlayer_workers.measure.telemetry import emit_evaluation_events

    events = _events(lambda: emit_evaluation_events(_make_score()))

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
    from nthlayer_workers.measure.telemetry import emit_evaluation_events

    alerts = [
        Alert(agent_name="agent-a", metric_name="reversal_rate",
              current_value=0.5, threshold=0.3, message="too high")
    ]
    events = _events(lambda: emit_evaluation_events(_make_score(), alerts))

    (summary,) = [a for n, a in events if n == "nthlayer.evaluation.completed"]
    assert summary["nthlayer.evaluation.id"] == "e1"
    assert summary["nthlayer.decision.id"] == "t1"
    assert summary["nthlayer.evaluation.dimension_count"] == 2
    assert summary["nthlayer.evaluation.cost_usd"] == 0.01
    assert summary["nthlayer.evaluation.alert_count"] == 1
    assert summary["nthlayer.evaluation.auto_approved"] is False


def test_auto_approved_output_is_not_a_grader_evaluation():
    """No evaluator ran, so the configured scores are not anyone's opinion."""
    from nthlayer_workers.measure.telemetry import emit_evaluation_events

    score = _make_score(auto_approved=True, evaluator_model="auto-approved", cost_usd=None)
    events = _events(lambda: emit_evaluation_events(score))

    assert [n for n, _ in events] == ["nthlayer.evaluation.completed"]
    summary = events[0][1]
    assert summary["nthlayer.evaluation.auto_approved"] is True
    assert "nthlayer.evaluation.cost_usd" not in summary
    assert "nthlayer.evaluation.alert_count" not in summary


def test_empty_task_id_is_not_emitted_as_a_decision_id():
    """A1 §4.4: omit nthlayer.decision.id when the decision is not identifiable."""
    from nthlayer_workers.measure.telemetry import emit_correction_event, emit_evaluation_events

    events = _events(lambda: emit_evaluation_events(_make_score(task_id="")))
    events += _events(lambda: emit_correction_event(
        "e1", "correctness", 0.9, 0.5, "r", corrected_at=EVENT_TIME, decision_id="",
    ))

    assert events
    assert not [n for n, a in events if "nthlayer.decision.id" in a]


def test_evaluation_with_no_dimensions_emits_only_the_summary():
    from nthlayer_workers.measure.telemetry import emit_evaluation_events

    events = _events(lambda: emit_evaluation_events(_make_score(dimensions={})))

    assert [n for n, _ in events] == ["nthlayer.evaluation.completed"]
    assert events[0][1]["nthlayer.evaluation.dimension_count"] == 0


def test_evaluation_records_are_stamped_with_the_evaluation_time():
    """A1 T1: every record of an evaluation carries when it was made."""
    from nthlayer_workers.measure.telemetry import emit_evaluation_events

    records = _records(lambda: emit_evaluation_events(_make_score(timestamp=EVENT_TIME)))

    assert len(records) == 3  # two dimensions and the summary
    assert {r.timestamp for r in records} == {EVENT_TIME_NS}


def test_correction_record_is_stamped_with_the_correction_time():
    from nthlayer_workers.measure.telemetry import emit_correction_event

    (record,) = _records(lambda: emit_correction_event(
        "e1", "correctness", 0.9, 0.5, "reviewer", corrected_at=EVENT_TIME,
    ))

    assert record.timestamp == EVENT_TIME_NS


def test_autonomy_record_is_stamped_with_the_change_time():
    from nthlayer_workers.measure.telemetry import emit_state_transition_event

    (record,) = _records(lambda: emit_state_transition_event(
        "agent-a", "full", "supervised", "governance", changed_at=EVENT_TIME,
    ))

    assert record.timestamp == EVENT_TIME_NS


def test_emitting_without_telemetry_initialised_does_not_raise():
    """Outside capture_events and before init_telemetry, emits are no-ops."""
    from nthlayer_workers.measure.telemetry import emit_evaluation_events

    emit_evaluation_events(_make_score())


def test_human_correction_is_a_human_grader_evaluation():
    """A1 §4.4: a human's verdict on a completion is a grader evaluation."""
    from nthlayer_workers.measure.telemetry import emit_correction_event

    events = _events(lambda: emit_correction_event(
        "e1", "correctness", 0.9, 0.5, "reviewer", corrected_at=EVENT_TIME, decision_id="t1",
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
        "agent-a", "full", "supervised", "governance", changed_at=EVENT_TIME,
    ))

    assert [n for n, _ in events] == ["nthlayer.autonomy.changed"]
    attrs = events[0][1]
    assert attrs["from_level"] == "full"
    assert attrs["to_level"] == "supervised"
