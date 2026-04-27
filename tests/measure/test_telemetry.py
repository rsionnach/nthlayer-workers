"""Tests for OTel telemetry instrumentation."""

from unittest.mock import MagicMock, patch


from nthlayer_workers.measure.detection.protocol import Alert
from nthlayer_workers.measure.types import QualityScore


def _make_score(**kwargs) -> QualityScore:
    defaults = dict(
        eval_id="e1",
        agent_name="agent-a",
        task_id="t1",
        dimensions={"correctness": 0.9},
        confidence=0.85,
        evaluator_model="test-model",
        cost_usd=0.01,
    )
    defaults.update(kwargs)
    return QualityScore(**defaults)


def test_emit_decision_event_with_otel():
    mock_span = MagicMock()
    with patch("nthlayer_workers.measure.telemetry._HAS_OTEL", True), \
         patch("nthlayer_workers.measure.telemetry.trace") as mock_trace:
        mock_trace.get_current_span.return_value = mock_span

        from nthlayer_workers.measure.telemetry import emit_decision_event
        score = _make_score()
        alerts = [
            Alert(agent_name="agent-a", metric_name="reversal_rate",
                  current_value=0.5, threshold=0.3, message="too high")
        ]
        emit_decision_event(score, alerts)

        mock_span.add_event.assert_called_once()
        call_args = mock_span.add_event.call_args
        assert call_args[0][0] == "gen_ai.decision.evaluated"
        attrs = call_args[1]["attributes"]
        assert attrs["eval_id"] == "e1"
        assert attrs["alert_count"] == 1


def test_noop_without_otel():
    with patch("nthlayer_workers.measure.telemetry._HAS_OTEL", False):
        from nthlayer_workers.measure.telemetry import emit_decision_event
        # Should not raise even with no OTel
        emit_decision_event(_make_score())


def test_emit_override_event():
    mock_span = MagicMock()
    with patch("nthlayer_workers.measure.telemetry._HAS_OTEL", True), \
         patch("nthlayer_workers.measure.telemetry.trace") as mock_trace:
        mock_trace.get_current_span.return_value = mock_span

        from nthlayer_workers.measure.telemetry import emit_override_event
        emit_override_event("e1", "correctness", 0.9, 0.5, "reviewer")

        mock_span.add_event.assert_called_once()
        call_args = mock_span.add_event.call_args
        assert call_args[0][0] == "gen_ai.override.applied"
        attrs = call_args[1]["attributes"]
        assert attrs["eval_id"] == "e1"
        assert attrs["corrected_score"] == 0.5


def test_emit_state_transition():
    mock_span = MagicMock()
    with patch("nthlayer_workers.measure.telemetry._HAS_OTEL", True), \
         patch("nthlayer_workers.measure.telemetry.trace") as mock_trace:
        mock_trace.get_current_span.return_value = mock_span

        from nthlayer_workers.measure.telemetry import emit_state_transition_event
        emit_state_transition_event("agent-a", "full", "supervised", "governance")

        mock_span.add_event.assert_called_once()
        call_args = mock_span.add_event.call_args
        assert call_args[0][0] == "gen_ai.agent.state.changed"
        attrs = call_args[1]["attributes"]
        assert attrs["from_level"] == "full"
        assert attrs["to_level"] == "supervised"
