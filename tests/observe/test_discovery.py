"""Tests for the discovery module."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from nthlayer_workers.observe.discovery.classifier import MetricClassifier
from nthlayer_workers.observe.discovery.client import MetricDiscoveryClient
from nthlayer_workers.observe.discovery.models import (
    DiscoveredMetric,
    DiscoveryResult,
    MetricType,
    TechnologyGroup,
)


class TestModels:
    def test_metric_type_values(self):
        assert MetricType.COUNTER.value == "counter"
        assert MetricType.GAUGE.value == "gauge"
        assert MetricType.HISTOGRAM.value == "histogram"
        assert MetricType.UNKNOWN.value == "unknown"

    def test_technology_group_values(self):
        assert TechnologyGroup.POSTGRESQL.value == "postgresql"
        assert TechnologyGroup.REDIS.value == "redis"
        assert TechnologyGroup.HTTP.value == "http"
        assert TechnologyGroup.CUSTOM.value == "custom"

    def test_discovered_metric_defaults(self):
        m = DiscoveredMetric(name="http_requests_total")
        assert m.type == MetricType.UNKNOWN
        assert m.technology == TechnologyGroup.UNKNOWN
        assert m.help_text is None
        assert m.labels == {}

    def test_discovery_result_defaults(self):
        r = DiscoveryResult(service="svc", total_metrics=0)
        assert r.metrics == []
        assert r.metrics_by_technology == {}
        assert r.metrics_by_type == {}


class TestMetricClassifier:
    def test_classify_postgresql(self):
        classifier = MetricClassifier()
        m = DiscoveredMetric(name="pg_stat_activity")
        classified = classifier.classify(m)
        assert classified.technology == TechnologyGroup.POSTGRESQL

    def test_classify_redis(self):
        classifier = MetricClassifier()
        m = DiscoveredMetric(name="redis_connected_clients")
        classified = classifier.classify(m)
        assert classified.technology == TechnologyGroup.REDIS

    def test_classify_http(self):
        classifier = MetricClassifier()
        m = DiscoveredMetric(name="http_requests_total")
        classified = classifier.classify(m)
        assert classified.technology == TechnologyGroup.HTTP

    def test_classify_kubernetes(self):
        classifier = MetricClassifier()
        m = DiscoveredMetric(name="kube_pod_status_phase")
        classified = classifier.classify(m)
        assert classified.technology == TechnologyGroup.KUBERNETES

    def test_classify_custom(self):
        classifier = MetricClassifier()
        m = DiscoveredMetric(name="my_app_custom_metric")
        classified = classifier.classify(m)
        assert classified.technology == TechnologyGroup.CUSTOM

    def test_infer_counter_type(self):
        classifier = MetricClassifier()
        m = DiscoveredMetric(name="http_requests_total")
        classified = classifier.classify(m)
        assert classified.type == MetricType.COUNTER

    def test_infer_histogram_type(self):
        classifier = MetricClassifier()
        m = DiscoveredMetric(name="http_request_duration_seconds_bucket")
        classified = classifier.classify(m)
        assert classified.type == MetricType.HISTOGRAM

    def test_infer_gauge_default(self):
        classifier = MetricClassifier()
        m = DiscoveredMetric(name="some_metric")
        classified = classifier.classify(m)
        assert classified.type == MetricType.GAUGE

    def test_does_not_override_known_type(self):
        classifier = MetricClassifier()
        m = DiscoveredMetric(name="http_requests_total", type=MetricType.GAUGE)
        classified = classifier.classify(m)
        assert classified.type == MetricType.GAUGE  # not overridden


class TestMetricDiscoveryClient:
    def test_initialization(self):
        client = MetricDiscoveryClient("http://prom:9090/")
        assert client.prometheus_url == "http://prom:9090"

    def test_initialization_with_auth(self):
        client = MetricDiscoveryClient("http://prom:9090", username="u", password="p")
        assert client.auth == ("u", "p")

    def test_initialization_with_bearer(self):
        client = MetricDiscoveryClient("http://prom:9090", bearer_token="tok")
        assert client.headers == {"Authorization": "Bearer tok"}

    def test_extract_service_from_selector(self):
        client = MetricDiscoveryClient("http://prom:9090")
        assert client._extract_service_from_selector('{service="payment-api"}') == "payment-api"
        assert client._extract_service_from_selector("{}") == "unknown"

    def test_discover_with_mocked_prometheus(self):
        client = MetricDiscoveryClient("http://prom:9090")

        series_response = MagicMock()
        series_response.status_code = 200
        series_response.raise_for_status = MagicMock()
        series_response.json.return_value = {
            "status": "success",
            "data": [
                {"__name__": "http_requests_total", "service": "foo"},
                {"__name__": "http_request_duration_seconds_bucket", "service": "foo"},
            ],
        }

        metadata_response = MagicMock()
        metadata_response.status_code = 200
        metadata_response.raise_for_status = MagicMock()
        metadata_response.json.return_value = {"status": "success", "data": {}}

        with patch("httpx.get") as mock_get:
            mock_get.return_value = series_response

            result = client.discover('{service="foo"}')

            assert result.total_metrics == 2
            assert result.service == "foo"

    def test_discover_empty_result(self):
        client = MetricDiscoveryClient("http://prom:9090")

        response = MagicMock()
        response.status_code = 200
        response.raise_for_status = MagicMock()
        response.json.return_value = {"status": "success", "data": []}

        with patch("httpx.get", return_value=response):
            result = client.discover('{service="foo"}')
            assert result.total_metrics == 0

    def test_discover_handles_connection_error(self):
        client = MetricDiscoveryClient("http://prom:9090")

        with patch("httpx.get", side_effect=Exception("connection refused")):
            result = client.discover('{service="foo"}')
            assert result.total_metrics == 0


class TestDiscoverCLI:
    def test_discover_help(self, capsys):
        from nthlayer_workers.observe.cli import main

        with pytest.raises(SystemExit) as exc_info:
            main(["discover", "--help"])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "--prometheus-url" in captured.out
        assert "--service" in captured.out
        assert "--format" in captured.out
