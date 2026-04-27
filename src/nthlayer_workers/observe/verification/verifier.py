"""Prometheus metric verifier."""

from __future__ import annotations

import os

import httpx
import structlog

from nthlayer_workers.observe.verification.models import (
    ContractVerificationResult,
    DeclaredMetric,
    MetricContract,
    VerificationResult,
)

logger = structlog.get_logger()


class MetricVerifier:
    """Verifies metrics exist in Prometheus."""

    def __init__(
        self,
        prometheus_url: str,
        username: str | None = None,
        password: str | None = None,
        timeout: float = 30.0,
    ):
        self.prometheus_url = prometheus_url.rstrip("/")
        self.auth = (username, password) if username and password else None
        self.timeout = timeout

        if not self.auth:
            env_user = os.environ.get("PROMETHEUS_USERNAME")
            env_pass = os.environ.get("PROMETHEUS_PASSWORD")
            if env_user and env_pass:
                self.auth = (env_user, env_pass)

    def verify_contract(self, contract: MetricContract) -> ContractVerificationResult:
        """Verify all metrics in a contract exist."""
        result = ContractVerificationResult(
            service_name=contract.service_name,
            target_url=self.prometheus_url,
        )

        for metric in contract.metrics:
            verification = self.verify_metric(metric, contract.service_name)
            result.results.append(verification)

        return result

    def verify_metric(self, metric: DeclaredMetric, service_name: str) -> VerificationResult:
        """Verify a single metric exists in Prometheus."""
        try:
            exists, sample_labels = self._check_metric_exists(metric.name, service_name)
            return VerificationResult(metric=metric, exists=exists, sample_labels=sample_labels)
        except Exception as e:
            logger.warning("metric_verification_failed", metric=metric.name, error=str(e))
            return VerificationResult(metric=metric, exists=False, error=str(e))

    def _check_metric_exists(
        self, metric_name: str, service_name: str
    ) -> tuple[bool, dict | None]:
        """Check if a metric exists. Tries with service label, then without."""
        selector = f'{metric_name}{{service="{service_name}"}}'
        exists, labels = self._query_series(selector)
        if exists:
            return True, labels

        selector = f"{metric_name}"
        return self._query_series(selector)

    def _query_series(self, selector: str) -> tuple[bool, dict | None]:
        """Query Prometheus series API."""
        url = f"{self.prometheus_url}/api/v1/series"
        params: dict[str, str | int] = {"match[]": selector, "limit": 1}

        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.get(url, params=params, auth=self.auth)
                response.raise_for_status()
                data = response.json()

                if data.get("status") == "success":
                    series = data.get("data", [])
                    if series:
                        sample = series[0]
                        labels = {k: v for k, v in sample.items() if k != "__name__"}
                        return True, labels

                return False, None

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return False, None
            raise
        except httpx.ConnectError as e:
            raise ConnectionError(f"Cannot connect to Prometheus at {self.prometheus_url}") from e
        except httpx.TimeoutException as e:
            raise TimeoutError(f"Timeout connecting to Prometheus at {self.prometheus_url}") from e

    def test_connection(self) -> bool:
        """Test connection to Prometheus."""
        try:
            url = f"{self.prometheus_url}/api/v1/status/buildinfo"
            with httpx.Client(timeout=10.0) as client:
                response = client.get(url, auth=self.auth)
                return response.status_code == 200
        except Exception:
            return False
