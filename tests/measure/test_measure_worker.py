"""Tests for MeasureModule — three-type output from judgment SLO evaluation."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch


from nthlayer_common.api_client import APIResult
from nthlayer_workers.measure.worker import (
    MeasureModule,
    _detect_transitions,
    _reduce_autonomy,
    classify_severity,
)


def _manifest_with_judgment_slos():
    return APIResult(ok=True, status_code=200, data=[
        {
            "name": "fraud-detect",
            "tier": "critical",
            "type": "ai-gate",
            "slos": [
                {
                    "name": "reversal_rate",
                    "target": 0.985,
                    "slo_type": "availability",
                    "window": "2m",
                    "judgment_type": "reversal_rate",
                    "indicator_query": '1 - (sum(rate(gen_ai_overrides_total[2m])) / clamp_min(sum(rate(gen_ai_decisions_total[2m])), 1))',
                },
            ],
            "dependencies": [],
            "contracts": [],
        },
    ])


def _manifest_no_judgment_slos():
    return APIResult(ok=True, status_code=200, data=[
        {
            "name": "payment-api",
            "tier": "critical",
            "type": "api",
            "slos": [
                {"name": "availability", "target": 99.9, "slo_type": "availability", "window": "30d"},
            ],
            "dependencies": [],
            "contracts": [],
        },
    ])


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class TestMeasureModuleProtocol:
    def test_name(self):
        module = MeasureModule(client=AsyncMock(), prometheus_url="http://prom:9090")
        assert module.name == "measure"

    async def test_restore_state_accepts_none(self):
        module = MeasureModule(client=AsyncMock(), prometheus_url="http://prom:9090")
        await module.restore_state(None)

    async def test_restore_state_restores_all_fields(self):
        module = MeasureModule(client=AsyncMock(), prometheus_url="http://prom:9090")
        await module.restore_state({
            "slo_status": {"fraud-detect:reversal_rate": "breach"},
            "breach_decisions": {},
            "autonomy_levels": {"fraud-detect": "limited_autonomous"},
            "breach_severities": {"fraud-detect:reversal_rate": "high"},
        })
        assert module._slo_status == {"fraud-detect:reversal_rate": "breach"}
        assert module._autonomy_levels == {"fraud-detect": "limited_autonomous"}
        assert module._breach_severities == {"fraud-detect:reversal_rate": "high"}

    async def test_get_state_includes_all_fields(self):
        module = MeasureModule(client=AsyncMock(), prometheus_url="http://prom:9090")
        module._slo_status = {"svc:slo": "healthy"}
        module._breach_decisions = {"svc:slo": {"decided": True}}
        module._autonomy_levels = {"svc": "full"}
        state = await module.get_state()
        assert "slo_status" in state
        assert "breach_decisions" in state
        assert "breach_severities" in state
        assert "autonomy_levels" in state

    def test_satisfies_protocol(self):
        from nthlayer_workers.runner import WorkerModule
        module = MeasureModule(client=AsyncMock(), prometheus_url="http://prom:9090")
        assert isinstance(module, WorkerModule)


# ---------------------------------------------------------------------------
# Phase 1: Evaluation
# ---------------------------------------------------------------------------


class TestEvaluation:
    @patch("nthlayer_workers.measure.worker.PrometheusProvider")
    async def test_evaluation_produces_assessment(self, mock_prov_cls):
        mock_prov = AsyncMock()
        mock_prov.get_sli_value = AsyncMock(return_value=0.99)
        mock_prov.aclose = AsyncMock()
        mock_prov_cls.return_value = mock_prov

        client = AsyncMock()
        client.get_manifests = AsyncMock(return_value=_manifest_with_judgment_slos())
        submitted = []
        client.submit_assessment = AsyncMock(
            side_effect=lambda a: (submitted.append(a), APIResult(ok=True, status_code=201, data={}))[1]
        )
        client.submit_verdict = AsyncMock(return_value=APIResult(ok=True, status_code=201, data={}))

        module = MeasureModule(client=client, prometheus_url="http://prom:9090")
        await module.process_cycle()

        assessments = [a for a in submitted if a.get("kind") == "judgment_slo_evaluation"]
        assert len(assessments) == 1
        assert assessments[0]["service"] == "fraud-detect"
        assert assessments[0]["data"]["status"] == "healthy"

    @patch("nthlayer_workers.measure.worker.PrometheusProvider")
    async def test_evaluation_breach(self, mock_prov_cls):
        mock_prov = AsyncMock()
        mock_prov.get_sli_value = AsyncMock(return_value=0.92)  # below 0.985 target
        mock_prov.aclose = AsyncMock()
        mock_prov_cls.return_value = mock_prov

        client = AsyncMock()
        client.get_manifests = AsyncMock(return_value=_manifest_with_judgment_slos())
        submitted = []
        client.submit_assessment = AsyncMock(
            side_effect=lambda a: (submitted.append(a), APIResult(ok=True, status_code=201, data={}))[1]
        )
        client.submit_verdict = AsyncMock(return_value=APIResult(ok=True, status_code=201, data={}))

        module = MeasureModule(client=client, prometheus_url="http://prom:9090")
        await module.process_cycle()

        assessments = [a for a in submitted if a.get("kind") == "judgment_slo_evaluation"]
        assert assessments[0]["data"]["status"] == "breach"

    @patch("nthlayer_workers.measure.worker.PrometheusProvider")
    async def test_no_judgment_slos_no_assessment(self, mock_prov_cls):
        mock_prov = AsyncMock()
        mock_prov.aclose = AsyncMock()
        mock_prov_cls.return_value = mock_prov

        client = AsyncMock()
        client.get_manifests = AsyncMock(return_value=_manifest_no_judgment_slos())
        client.submit_assessment = AsyncMock()
        client.submit_verdict = AsyncMock()

        module = MeasureModule(client=client, prometheus_url="http://prom:9090")
        await module.process_cycle()

        client.submit_assessment.assert_not_awaited()


# ---------------------------------------------------------------------------
# Phase 2: Breach transitions
# ---------------------------------------------------------------------------


class TestBreachTransitions:
    @patch("nthlayer_workers.measure.worker.PrometheusProvider")
    async def test_healthy_to_breach_emits_verdict(self, mock_prov_cls):
        mock_prov = AsyncMock()
        mock_prov.get_sli_value = AsyncMock(return_value=0.92)  # breach
        mock_prov.aclose = AsyncMock()
        mock_prov_cls.return_value = mock_prov

        client = AsyncMock()
        client.get_manifests = AsyncMock(return_value=_manifest_with_judgment_slos())
        client.submit_assessment = AsyncMock(return_value=APIResult(ok=True, status_code=201, data={}))
        verdicts = []
        client.submit_verdict = AsyncMock(
            side_effect=lambda v: (verdicts.append(v), APIResult(ok=True, status_code=201, data={}))[1]
        )

        module = MeasureModule(client=client, prometheus_url="http://prom:9090")
        # First cycle: unknown→breach = transition
        await module.process_cycle()

        breach_verdicts = [v for v in verdicts if v.get("type") == "quality_breach"]
        assert len(breach_verdicts) == 1
        assert breach_verdicts[0]["service"] == "fraud-detect"

    @patch("nthlayer_workers.measure.worker.PrometheusProvider")
    async def test_breach_to_breach_no_verdict(self, mock_prov_cls):
        mock_prov = AsyncMock()
        mock_prov.get_sli_value = AsyncMock(return_value=0.92)
        mock_prov.aclose = AsyncMock()
        mock_prov_cls.return_value = mock_prov

        client = AsyncMock()
        client.get_manifests = AsyncMock(return_value=_manifest_with_judgment_slos())
        client.submit_assessment = AsyncMock(return_value=APIResult(ok=True, status_code=201, data={}))
        verdicts = []
        client.submit_verdict = AsyncMock(
            side_effect=lambda v: (verdicts.append(v), APIResult(ok=True, status_code=201, data={}))[1]
        )

        # Pre-set state: already breaching
        module = MeasureModule(client=client, prometheus_url="http://prom:9090")
        module._slo_status = {"fraud-detect:reversal_rate": "breach"}
        module._breach_decisions = {"fraud-detect:reversal_rate": {"decided": True}}

        await module.process_cycle()

        breach_verdicts = [v for v in verdicts if v.get("type") == "quality_breach"]
        assert len(breach_verdicts) == 0

    @patch("nthlayer_workers.measure.worker.PrometheusProvider")
    async def test_breach_to_healthy_clears_decision(self, mock_prov_cls):
        mock_prov = AsyncMock()
        mock_prov.get_sli_value = AsyncMock(return_value=0.99)  # healthy
        mock_prov.aclose = AsyncMock()
        mock_prov_cls.return_value = mock_prov

        client = AsyncMock()
        client.get_manifests = AsyncMock(return_value=_manifest_with_judgment_slos())
        client.submit_assessment = AsyncMock(return_value=APIResult(ok=True, status_code=201, data={}))
        client.submit_verdict = AsyncMock()

        module = MeasureModule(client=client, prometheus_url="http://prom:9090")
        module._slo_status = {"fraud-detect:reversal_rate": "breach"}
        module._breach_decisions = {"fraud-detect:reversal_rate": {"decided": True}}

        await module.process_cycle()

        # Breach decision should be cleared on recovery
        assert "fraud-detect:reversal_rate" not in module._breach_decisions
        assert module._slo_status.get("fraud-detect:reversal_rate") == "healthy"


# ---------------------------------------------------------------------------
# Phase 3: Autonomy governance
# ---------------------------------------------------------------------------


class TestAutonomyGovernance:
    @patch("nthlayer_workers.measure.worker.PrometheusProvider")
    async def test_breach_triggers_governance(self, mock_prov_cls):
        mock_prov = AsyncMock()
        mock_prov.get_sli_value = AsyncMock(return_value=0.92)
        mock_prov.aclose = AsyncMock()
        mock_prov_cls.return_value = mock_prov

        client = AsyncMock()
        client.get_manifests = AsyncMock(return_value=_manifest_with_judgment_slos())
        client.submit_assessment = AsyncMock(return_value=APIResult(ok=True, status_code=201, data={}))
        verdicts = []
        client.submit_verdict = AsyncMock(
            side_effect=lambda v: (verdicts.append(v), APIResult(ok=True, status_code=201, data={}))[1]
        )

        module = MeasureModule(client=client, prometheus_url="http://prom:9090")
        await module.process_cycle()

        auto_verdicts = [v for v in verdicts if v.get("type") == "autonomy_change"]
        assert len(auto_verdicts) == 1
        assert auto_verdicts[0]["data"]["previous_level"] == "fully_autonomous"
        assert auto_verdicts[0]["data"]["new_level"] == "limited_autonomous"

    @patch("nthlayer_workers.measure.worker.PrometheusProvider")
    async def test_subsequent_cycles_no_governance(self, mock_prov_cls):
        """Breach continues but governance already decided — no re-decision."""
        mock_prov = AsyncMock()
        mock_prov.get_sli_value = AsyncMock(return_value=0.92)
        mock_prov.aclose = AsyncMock()
        mock_prov_cls.return_value = mock_prov

        client = AsyncMock()
        client.get_manifests = AsyncMock(return_value=_manifest_with_judgment_slos())
        client.submit_assessment = AsyncMock(return_value=APIResult(ok=True, status_code=201, data={}))
        verdicts = []
        client.submit_verdict = AsyncMock(
            side_effect=lambda v: (verdicts.append(v), APIResult(ok=True, status_code=201, data={}))[1]
        )

        module = MeasureModule(client=client, prometheus_url="http://prom:9090")
        module._slo_status = {"fraud-detect:reversal_rate": "breach"}
        module._breach_decisions = {"fraud-detect:reversal_rate": {"decided": True}}

        await module.process_cycle()

        auto_verdicts = [v for v in verdicts if v.get("type") == "autonomy_change"]
        assert len(auto_verdicts) == 0


class TestRebreachAfterRecovery:
    """Breach → healthy → re-breach should ratchet autonomy further."""

    @patch("nthlayer_workers.measure.worker.PrometheusProvider")
    async def test_rebrach_ratchets_from_previous_level(self, mock_prov_cls):
        mock_prov = AsyncMock()
        mock_prov.aclose = AsyncMock()
        mock_prov_cls.return_value = mock_prov

        client = AsyncMock()
        client.get_manifests = AsyncMock(return_value=_manifest_with_judgment_slos())
        client.submit_assessment = AsyncMock(return_value=APIResult(ok=True, status_code=201, data={}))
        verdicts = []
        client.submit_verdict = AsyncMock(
            side_effect=lambda v: (verdicts.append(v), APIResult(ok=True, status_code=201, data={}))[1]
        )

        module = MeasureModule(client=client, prometheus_url="http://prom:9090")
        # Pre-set: was breaching, autonomy already reduced to supervised
        module._slo_status = {"fraud-detect:reversal_rate": "breach"}
        module._breach_decisions = {"fraud-detect:reversal_rate": {"decided": True}}
        module._autonomy_levels = {"fraud-detect": "limited_autonomous"}

        # Cycle 1: recovery (healthy)
        mock_prov.get_sli_value = AsyncMock(return_value=0.99)
        await module.process_cycle()
        assert "fraud-detect:reversal_rate" not in module._breach_decisions

        # Cycle 2: re-breach
        verdicts.clear()
        mock_prov.get_sli_value = AsyncMock(return_value=0.92)
        await module.process_cycle()

        auto_verdicts = [v for v in verdicts if v.get("type") == "autonomy_change"]
        assert len(auto_verdicts) == 1
        # Ratchet continues from supervised → advisory_only
        assert auto_verdicts[0]["data"]["previous_level"] == "limited_autonomous"
        assert auto_verdicts[0]["data"]["new_level"] == "observer"


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


class TestFailureModes:
    async def test_manifest_fetch_failure_no_crash(self):
        client = AsyncMock()
        client.get_manifests = AsyncMock(
            return_value=APIResult(ok=False, status_code=503, error="unavailable")
        )
        module = MeasureModule(client=client, prometheus_url="http://prom:9090")
        await module.process_cycle()

    @patch("nthlayer_workers.measure.worker.PrometheusProvider")
    async def test_prometheus_failure_no_crash(self, mock_prov_cls):
        mock_prov = AsyncMock()
        mock_prov.get_sli_value = AsyncMock(side_effect=Exception("connection refused"))
        mock_prov.aclose = AsyncMock()
        mock_prov_cls.return_value = mock_prov

        client = AsyncMock()
        client.get_manifests = AsyncMock(return_value=_manifest_with_judgment_slos())
        client.submit_assessment = AsyncMock()
        client.submit_verdict = AsyncMock()

        module = MeasureModule(client=client, prometheus_url="http://prom:9090")
        await module.process_cycle()  # should not crash


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestDetectTransitions:
    def test_healthy_to_breach(self):
        assert _detect_transitions(
            {"svc:slo": "breach"}, {"svc:slo": "healthy"}
        ) == ["svc:slo"]

    def test_breach_to_breach(self):
        assert _detect_transitions(
            {"svc:slo": "breach"}, {"svc:slo": "breach"}
        ) == []

    def test_breach_to_healthy(self):
        assert _detect_transitions(
            {"svc:slo": "healthy"}, {"svc:slo": "breach"}
        ) == []

    def test_unknown_to_breach(self):
        assert _detect_transitions(
            {"svc:slo": "breach"}, {}
        ) == ["svc:slo"]

    def test_healthy_to_healthy(self):
        assert _detect_transitions(
            {"svc:slo": "healthy"}, {"svc:slo": "healthy"}
        ) == []

    def test_unknown_to_healthy_no_transition(self):
        """Cold start with healthy SLO should NOT fire a breach."""
        assert _detect_transitions(
            {"svc:slo": "healthy"}, {}
        ) == []


class TestReduceAutonomy:
    def test_one_step(self):
        assert _reduce_autonomy("fully_autonomous", 1) == "autonomous"

    def test_two_steps(self):
        assert _reduce_autonomy("fully_autonomous", 2) == "limited_autonomous"

    def test_drop_to_observer(self):
        assert _reduce_autonomy("autonomous", -1) == "observer"

    def test_drop_to_advisor(self):
        assert _reduce_autonomy("autonomous", -2) == "advisor"

    def test_observer_stays_observer(self):
        assert _reduce_autonomy("observer", 1) == "observer"

    def test_never_promotes_from_observer(self):
        """One-way ratchet: observer stays observer even with steps=-2 (advisor)."""
        assert _reduce_autonomy("observer", -2) == "observer"

    def test_never_promotes_from_advisor(self):
        """advisor + steps=-2 stays advisor (not promoted)."""
        assert _reduce_autonomy("advisor", -2) == "advisor"

    def test_unknown_to_advisor(self):
        assert _reduce_autonomy("unknown_level", 1) == "advisor"


class TestClassifySeverity:
    def test_budget_consumption_low(self):
        # target=0.985, value=0.98 → (0.985-0.98)/(1-0.985)*100 = 33% → low
        assert classify_severity("reversal_rate", 0.985, 0.98) == "low"

    def test_budget_consumption_high(self):
        # target=0.985, value=0.92 → 433% → high
        assert classify_severity("reversal_rate", 0.985, 0.92) == "high"

    def test_budget_consumption_critical(self):
        # target=0.985, value=0.90 → 567% → critical
        assert classify_severity("reversal_rate", 0.985, 0.90) == "critical"

    def test_stability_is_high(self):
        assert classify_severity("stability", 1.0, 0.0) == "high"

    def test_calibration_low(self):
        assert classify_severity("calibration", 0.5, 0.55) == "low"

    def test_calibration_high(self):
        assert classify_severity("calibration", 0.5, 0.7) == "high"

    def test_calibration_critical(self):
        assert classify_severity("calibration", 0.5, 0.9) == "critical"

    def test_budget_consumption_target_one_is_critical(self):
        # target=1.0 means zero error budget — any miss is critical
        assert classify_severity("reversal_rate", 1.0, 0.99) == "critical"

    def test_unknown_type_defaults_high(self):
        assert classify_severity("custom_type", 0.99, 0.5) == "high"
