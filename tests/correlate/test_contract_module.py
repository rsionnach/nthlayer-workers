"""Tests for CorrelateContractModule — contract divergence detection."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from nthlayer_common.api_client import APIResult
from nthlayer_workers.correlate.worker import CorrelateContractModule


def _manifest_with_contracts():
    return APIResult(ok=True, status_code=200, data=[
        {
            "name": "payment-api",
            "tier": "critical",
            "type": "api",
            "slos": [
                {
                    "name": "availability",
                    "target": 99.99,
                    "slo_type": "availability",
                    "window": "30d",
                    "indicator_query": 'sum(rate(http_requests_total{service="payment-api",status="200"}[5m])) / sum(rate(http_requests_total{service="payment-api"}[5m]))',
                },
            ],
            "dependencies": [],
            "contracts": [
                {
                    "name": "payment-api-contract",
                    "promise": {
                        "availability": 0.9999,
                        "latency_p99": "200ms",
                    },
                },
            ],
        },
    ])


def _manifest_no_contracts():
    return APIResult(ok=True, status_code=200, data=[
        {
            "name": "analytics-api",
            "tier": "standard",
            "type": "api",
            "slos": [],
            "dependencies": [],
            "contracts": [],
        },
    ])


class TestContractModuleProtocol:
    def test_name(self):
        module = CorrelateContractModule(client=AsyncMock(), prometheus_url="http://prom:9090")
        assert module.name == "correlate.contract"

    async def test_restore_state_accepts_none(self):
        module = CorrelateContractModule(client=AsyncMock(), prometheus_url="http://prom:9090")
        await module.restore_state(None)

    async def test_get_state_empty(self):
        module = CorrelateContractModule(client=AsyncMock(), prometheus_url="http://prom:9090")
        assert await module.get_state() == {}

    def test_satisfies_protocol(self):
        from nthlayer_workers.runner import WorkerModule
        module = CorrelateContractModule(client=AsyncMock(), prometheus_url="http://prom:9090")
        assert isinstance(module, WorkerModule)


class TestContractCycle:
    async def test_no_contracts_no_assessment(self):
        client = AsyncMock()
        client.get_manifests = AsyncMock(return_value=_manifest_no_contracts())
        client.submit_assessment = AsyncMock()

        module = CorrelateContractModule(client=client, prometheus_url="http://prom:9090")
        await module.process_cycle()

        client.submit_assessment.assert_not_awaited()

    async def test_manifest_fetch_fails_no_crash(self):
        client = AsyncMock()
        client.get_manifests = AsyncMock(
            return_value=APIResult(ok=False, status_code=503, error="unavailable")
        )
        client.submit_assessment = AsyncMock()

        module = CorrelateContractModule(client=client, prometheus_url="http://prom:9090")
        await module.process_cycle()

        client.submit_assessment.assert_not_awaited()

    @patch("nthlayer_common.providers.PrometheusProvider")
    async def test_divergence_detected(self, mock_prov_cls):
        """Observed availability below promised → contract_divergence emitted."""
        mock_prov = AsyncMock()
        # Return 99.85% as ratio (0.9985) — below promised 0.9999
        mock_prov.get_sli_value = AsyncMock(return_value=0.9985)
        mock_prov.aclose = AsyncMock()
        mock_prov_cls.return_value = mock_prov

        client = AsyncMock()
        client.get_manifests = AsyncMock(return_value=_manifest_with_contracts())
        submitted = []
        client.submit_assessment = AsyncMock(
            side_effect=lambda a: (submitted.append(a), APIResult(ok=True, status_code=201, data={"id": "x"}))[1]
        )

        module = CorrelateContractModule(client=client, prometheus_url="http://prom:9090")
        await module.process_cycle()

        assert len(submitted) == 1
        assert submitted[0]["kind"] == "contract_divergence"
        assert submitted[0]["service"] == "payment-api"
        divs = submitted[0]["data"]["divergences"]
        assert any(d["metric"] == "availability" for d in divs)

    @patch("nthlayer_common.providers.PrometheusProvider")
    async def test_no_divergence_no_assessment(self, mock_prov_cls):
        """Observed meets promises → no assessment."""
        mock_prov = AsyncMock()
        # Return 99.995% as ratio — above promised 0.9999
        mock_prov.get_sli_value = AsyncMock(return_value=0.99995)
        mock_prov.aclose = AsyncMock()
        mock_prov_cls.return_value = mock_prov

        client = AsyncMock()
        client.get_manifests = AsyncMock(return_value=_manifest_with_contracts())
        client.submit_assessment = AsyncMock()

        module = CorrelateContractModule(client=client, prometheus_url="http://prom:9090")
        await module.process_cycle()

        client.submit_assessment.assert_not_awaited()

    @patch("nthlayer_common.providers.PrometheusProvider")
    async def test_zero_sli_treated_as_total_outage_emits_divergence(self, mock_prov_cls):
        """SLI value of 0.0 is a total outage — contract promise IS violated.

        Per opensrm-e1gk, 0.0 is treated as valid data (not no-data).
        A total outage violates any availability contract.
        """
        mock_prov = AsyncMock()
        mock_prov.get_sli_value = AsyncMock(return_value=0.0)
        mock_prov.aclose = AsyncMock()
        mock_prov_cls.return_value = mock_prov

        client = AsyncMock()
        client.get_manifests = AsyncMock(return_value=_manifest_with_contracts())
        client.submit_assessment = AsyncMock()

        module = CorrelateContractModule(client=client, prometheus_url="http://prom:9090")
        await module.process_cycle()

        assert client.submit_assessment.await_count == 1
