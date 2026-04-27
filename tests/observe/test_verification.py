"""Tests for the verification module (adapted from nthlayer/tests/test_verification.py)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from nthlayer_workers.observe.verification.extractor import (
    Resource,
    _extract_metrics_from_query,
    extract_metric_contract,
)
from nthlayer_workers.observe.verification.exporter_guidance import (
    detect_missing_exporters,
    format_exporter_guidance,
    get_exporter_guidance,
)
from nthlayer_workers.observe.verification.models import (
    ContractVerificationResult,
    DeclaredMetric,
    MetricContract,
    MetricSource,
    VerificationResult,
)
from nthlayer_workers.observe.verification.verifier import MetricVerifier


class TestMetricExtraction:
    def test_extract_simple_metric(self):
        names = _extract_metrics_from_query('http_requests_total{service="foo"}')
        assert "http_requests_total" in names

    def test_extract_rate_metric(self):
        names = _extract_metrics_from_query('rate(http_requests_total{service="foo"}[5m])')
        assert "http_requests_total" in names
        assert "rate" not in names

    def test_extract_histogram_metric(self):
        names = _extract_metrics_from_query(
            'histogram_quantile(0.99, rate(http_request_duration_seconds_bucket{service="foo"}[5m]))'
        )
        assert "http_request_duration_seconds_bucket" in names
        assert "histogram_quantile" not in names
        assert "rate" not in names

    def test_extract_multiple_metrics(self):
        names = _extract_metrics_from_query(
            'sum(rate(good_requests{service="foo"}[5m])) / sum(rate(total_requests{service="foo"}[5m]))'
        )
        assert "good_requests" in names
        assert "total_requests" in names
        assert "sum" not in names

    def test_extract_filters_keywords(self):
        names = _extract_metrics_from_query("sum(rate(my_metric[5m]))")
        assert names == ["my_metric"]

    def test_extract_empty_query(self):
        assert _extract_metrics_from_query("") == []


class TestExtractMetricContract:
    def test_extract_slo_metrics(self):
        resources = [
            Resource(
                kind="SLO",
                name="availability",
                spec={
                    "indicators": [
                        {
                            "success_ratio": {
                                "total_query": 'http_requests_total{service="foo"}',
                                "good_query": 'http_requests_total{service="foo",status!~"5.."}',
                            }
                        }
                    ]
                },
            )
        ]
        contract = extract_metric_contract("foo", resources)
        assert len(contract.metrics) == 1  # http_requests_total (deduplicated)
        assert contract.metrics[0].source == MetricSource.SLO_INDICATOR
        assert contract.metrics[0].is_critical

    def test_extract_observability_metrics(self):
        resources = [
            Resource(
                kind="Observability",
                name="custom-metrics",
                spec={"metrics": ["custom_counter", "custom_gauge"]},
            )
        ]
        contract = extract_metric_contract("foo", resources)
        assert len(contract.metrics) == 2
        assert all(m.source == MetricSource.OBSERVABILITY for m in contract.metrics)
        assert not contract.metrics[0].is_critical

    def test_extract_deduplicates(self):
        resources = [
            Resource(
                kind="SLO",
                name="avail",
                spec={"indicators": [{"success_ratio": {"total_query": "http_requests_total[5m]"}}]},
            ),
            Resource(
                kind="Observability",
                name="obs",
                spec={"metrics": ["http_requests_total"]},
            ),
        ]
        contract = extract_metric_contract("foo", resources)
        assert len(contract.metrics) == 1  # deduplicated
        assert contract.metrics[0].source == MetricSource.SLO_INDICATOR  # first wins

    def test_skips_unknown_resource_kinds(self):
        resources = [Resource(kind="Dashboard", name="d", spec={})]
        contract = extract_metric_contract("foo", resources)
        assert len(contract.metrics) == 0

    def test_contract_properties(self):
        contract = MetricContract(
            service_name="foo",
            metrics=[
                DeclaredMetric(name="a", source=MetricSource.SLO_INDICATOR),
                DeclaredMetric(name="b", source=MetricSource.OBSERVABILITY),
            ],
        )
        assert len(contract.critical_metrics) == 1
        assert len(contract.optional_metrics) == 1
        assert contract.unique_metric_names == {"a", "b"}


class TestVerificationResult:
    def test_critical_failure(self):
        metric = DeclaredMetric(name="m", source=MetricSource.SLO_INDICATOR)
        result = VerificationResult(metric=metric, exists=False)
        assert result.is_critical_failure

    def test_optional_missing_not_critical(self):
        metric = DeclaredMetric(name="m", source=MetricSource.OBSERVABILITY)
        result = VerificationResult(metric=metric, exists=False)
        assert not result.is_critical_failure

    def test_existing_not_failure(self):
        metric = DeclaredMetric(name="m", source=MetricSource.SLO_INDICATOR)
        result = VerificationResult(metric=metric, exists=True)
        assert not result.is_critical_failure


class TestContractVerificationResult:
    def test_exit_code_all_verified(self):
        metric = DeclaredMetric(name="m", source=MetricSource.SLO_INDICATOR)
        result = ContractVerificationResult(
            service_name="foo",
            target_url="http://prom:9090",
            results=[VerificationResult(metric=metric, exists=True)],
        )
        assert result.exit_code == 0
        assert result.all_verified
        assert result.critical_verified

    def test_exit_code_optional_missing(self):
        result = ContractVerificationResult(
            service_name="foo",
            target_url="http://prom:9090",
            results=[
                VerificationResult(
                    metric=DeclaredMetric(name="m", source=MetricSource.OBSERVABILITY),
                    exists=False,
                )
            ],
        )
        assert result.exit_code == 1

    def test_exit_code_critical_missing(self):
        result = ContractVerificationResult(
            service_name="foo",
            target_url="http://prom:9090",
            results=[
                VerificationResult(
                    metric=DeclaredMetric(name="m", source=MetricSource.SLO_INDICATOR),
                    exists=False,
                )
            ],
        )
        assert result.exit_code == 2

    def test_verified_count(self):
        result = ContractVerificationResult(
            service_name="foo",
            target_url="http://prom:9090",
            results=[
                VerificationResult(metric=DeclaredMetric(name="a", source=MetricSource.SLO_INDICATOR), exists=True),
                VerificationResult(metric=DeclaredMetric(name="b", source=MetricSource.SLO_INDICATOR), exists=False),
                VerificationResult(metric=DeclaredMetric(name="c", source=MetricSource.OBSERVABILITY), exists=True),
            ],
        )
        assert result.verified_count == 2
        assert len(result.missing_critical) == 1
        assert not result.all_verified


class TestMetricVerifier:
    def test_initialization(self):
        verifier = MetricVerifier("http://prom:9090/", username="u", password="p")
        assert verifier.prometheus_url == "http://prom:9090"
        assert verifier.auth == ("u", "p")

    def test_test_connection_success(self):
        verifier = MetricVerifier("http://prom:9090")
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.get.return_value = mock_response
            mock_client_cls.return_value = mock_client

            assert verifier.test_connection() is True

    def test_test_connection_failure(self):
        verifier = MetricVerifier("http://prom:9090")

        with patch("httpx.Client") as mock_client_cls:
            mock_client_cls.side_effect = Exception("connection refused")
            assert verifier.test_connection() is False

    def test_verify_metric_exists(self):
        verifier = MetricVerifier("http://prom:9090")
        metric = DeclaredMetric(name="http_requests_total", source=MetricSource.SLO_INDICATOR)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "status": "success",
            "data": [{"__name__": "http_requests_total", "service": "foo", "instance": "a:8080"}],
        }

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.get.return_value = mock_response
            mock_client_cls.return_value = mock_client

            result = verifier.verify_metric(metric, "foo")
            assert result.exists
            assert result.sample_labels == {"service": "foo", "instance": "a:8080"}

    def test_verify_metric_missing(self):
        verifier = MetricVerifier("http://prom:9090")
        metric = DeclaredMetric(name="missing_metric", source=MetricSource.SLO_INDICATOR)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"status": "success", "data": []}

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.get.return_value = mock_response
            mock_client_cls.return_value = mock_client

            result = verifier.verify_metric(metric, "foo")
            assert not result.exists


class TestExporterGuidance:
    def test_detect_postgresql(self):
        result = detect_missing_exporters(["pg_stat_activity", "pg_locks"])
        assert "postgresql" in result
        assert len(result["postgresql"]) == 2

    def test_detect_redis(self):
        result = detect_missing_exporters(["redis_connected_clients"])
        assert "redis" in result

    def test_no_match(self):
        result = detect_missing_exporters(["custom_metric_total"])
        assert result == {}

    def test_get_guidance(self):
        lines = get_exporter_guidance("postgresql")
        assert len(lines) > 0
        assert any("helm" in line for line in lines)

    def test_get_guidance_unknown(self):
        assert get_exporter_guidance("unknown") == []

    def test_format_guidance_empty(self):
        assert format_exporter_guidance({}) == []

    def test_format_guidance_with_data(self):
        lines = format_exporter_guidance({"postgresql": ["pg_stat_activity"]})
        assert len(lines) > 0
        assert any("PostgreSQL" in line for line in lines)


class TestVerifyCLI:
    def test_verify_help(self, capsys):
        from nthlayer_workers.observe.cli import main

        with pytest.raises(SystemExit) as exc_info:
            main(["verify", "--help"])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "--specs-dir" in captured.out
        assert "--prometheus-url" in captured.out
