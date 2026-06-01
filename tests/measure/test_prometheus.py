# tests/test_prometheus.py
"""Tests for the Prometheus polling adapter with mocked HTTP responses."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from nthlayer_workers.measure.adapters.prometheus import (
    _judgment_slo_query,
    SLODefinition,
    count_consecutive_breaches,
    evaluate_slos,
    load_specs,
    query_firing_alerts,
    query_prometheus,
)

# --- Fixtures ---

SAMPLE_SPEC = """\
apiVersion: srm/v1
kind: ServiceReliabilityManifest
metadata:
  name: fraud-detect
  tier: critical
spec:
  type: ai-gate
  slos:
    availability:
      target: 99.9
      window: 30d
    reversal_rate:
      target: 0.05
      window: 7d
    latency:
      target: 100
      unit: ms
      percentile: p99
      window: 30d
"""


@pytest.fixture
def specs_dir(tmp_path):
    spec_file = tmp_path / "fraud-detect.yaml"
    spec_file.write_text(SAMPLE_SPEC)
    return tmp_path


@pytest.fixture
def verdict_store():
    from nthlayer_common.verdicts import MemoryStore
    return MemoryStore()


# --- load_specs tests ---

def test_load_specs_parses_slos(specs_dir):
    slos = load_specs(specs_dir)
    assert len(slos) == 3
    names = {s.slo_name for s in slos}
    assert names == {"availability", "reversal_rate", "latency"}


def test_load_specs_classifies_judgment_slos(specs_dir):
    slos = load_specs(specs_dir)
    by_name = {s.slo_name: s for s in slos}
    assert by_name["reversal_rate"].slo_type == "judgment"
    assert by_name["availability"].slo_type == "traditional"
    assert by_name["latency"].slo_type == "traditional"


def test_load_specs_normalizes_availability_target(specs_dir):
    slos = load_specs(specs_dir)
    avail = next(s for s in slos if s.slo_name == "availability")
    assert avail.target == pytest.approx(0.999)


def test_load_specs_builds_promql(specs_dir):
    slos = load_specs(specs_dir)
    rev = next(s for s in slos if s.slo_name == "reversal_rate")
    assert "gen_ai_overrides_total" in rev.query
    assert "gen_ai_decisions_total" in rev.query
    assert "fraud-detect" in rev.query


def test_load_specs_empty_dir(tmp_path):
    slos = load_specs(tmp_path)
    assert slos == []


# --- _judgment_slo_query (opensrm-y7dd: lookup-dict refactor) ---


def test_judgment_slo_query_reversal_rate():
    q = _judgment_slo_query("fraud-detect", "reversal_rate", "5m")
    assert "gen_ai_overrides_total" in q
    assert 'service="fraud-detect"' in q
    assert "[5m]" in q


def test_judgment_slo_query_high_confidence_failure():
    q = _judgment_slo_query("svc-a", "high_confidence_failure", "10m")
    assert "gen_ai_overrides_hcf_total" in q
    assert 'confidence_bucket="high"' in q


def test_judgment_slo_query_calibration_window_agnostic():
    # Calibration is a gauge: window argument is intentionally ignored.
    q5 = _judgment_slo_query("svc", "calibration", "5m")
    q1h = _judgment_slo_query("svc", "calibration", "1h")
    assert q5 == q1h
    assert q5 == 'gen_ai_calibration_error{service="svc"}'


def test_judgment_slo_query_unknown_returns_empty():
    # Unknown SLO name must take the warn+empty path, not raise nor
    # invoke a lambda. Guards the lookup-dict against future regressions
    # that drop the unknown-key fallback.
    assert _judgment_slo_query("svc", "made_up_slo", "5m") == ""


def test_judgment_slo_query_empty_slo_name_returns_empty():
    # Empty string is not a known key — same fallback path as unknown.
    assert _judgment_slo_query("svc", "", "5m") == ""


# --- query_firing_alerts tests ---

@pytest.mark.asyncio
async def test_query_firing_alerts_returns_firing():
    import httpx

    mock_response = httpx.Response(
        200,
        json={"data": {"alerts": [
            {"state": "firing", "labels": {"service": "fraud-detect", "alertname": "HighErrorRate", "severity": "critical"}, "activeAt": "2026-03-25T10:00:00Z"},
            {"state": "pending", "labels": {"service": "fraud-detect", "alertname": "Other"}},
            {"state": "firing", "labels": {"service": "payment-api", "alertname": "LatencyHigh", "severity": "warning"}, "activeAt": "2026-03-25T10:01:00Z"},
        ]}},
        request=httpx.Request("GET", "http://test/api/v1/alerts"),
    )

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response

    alerts = await query_firing_alerts(mock_client, "http://test")
    assert len(alerts) == 2  # only firing, not pending


@pytest.mark.asyncio
async def test_query_firing_alerts_filters_by_service():
    import httpx

    mock_response = httpx.Response(
        200,
        json={"data": {"alerts": [
            {"state": "firing", "labels": {"service": "fraud-detect", "alertname": "A"}, "activeAt": "2026-03-25T10:00:00Z"},
            {"state": "firing", "labels": {"service": "payment-api", "alertname": "B"}, "activeAt": "2026-03-25T10:01:00Z"},
        ]}},
        request=httpx.Request("GET", "http://test/api/v1/alerts"),
    )

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response

    alerts = await query_firing_alerts(mock_client, "http://test", service="fraud-detect")
    assert len(alerts) == 1
    assert alerts[0]["labels"]["service"] == "fraud-detect"


# --- query_prometheus tests ---

@pytest.mark.asyncio
async def test_query_prometheus_returns_value():
    import httpx

    mock_response = httpx.Response(
        200,
        json={"data": {"result": [{"value": [1234567890, "0.08"]}]}},
        request=httpx.Request("GET", "http://test/api/v1/query"),
    )

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response

    result = await query_prometheus(mock_client, "http://test", "some_query")
    assert result == pytest.approx(0.08)


@pytest.mark.asyncio
async def test_query_prometheus_returns_none_on_empty():
    import httpx

    mock_response = httpx.Response(
        200,
        json={"data": {"result": []}},
        request=httpx.Request("GET", "http://test/api/v1/query"),
    )

    with patch("nthlayer_workers.measure.adapters.prometheus.httpx.AsyncClient"):
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response

        result = await query_prometheus(mock_client, "http://test", "some_query")
        assert result is None


@pytest.mark.asyncio
async def test_query_prometheus_returns_none_on_nan():
    import httpx

    mock_response = httpx.Response(
        200,
        json={"data": {"result": [{"value": [1234567890, "NaN"]}]}},
        request=httpx.Request("GET", "http://test/api/v1/query"),
    )

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response

    result = await query_prometheus(mock_client, "http://test", "some_query")
    assert result is None


# --- count_consecutive_breaches tests ---

def test_consecutive_breaches_counts_from_newest():
    from nthlayer_common.verdicts import create

    verdicts = []
    for i in range(5):
        v = create(
            subject={"type": "evaluation", "ref": "fraud-detect", "summary": f"test {i}"},
            judgment={"action": "flag", "confidence": 0.9},
            producer={"system": "nthlayer-measure"},
            metadata={"custom": {"slo_name": "reversal_rate", "breach": True, "current_value": 0.08, "target": 0.05}},
        )
        verdicts.append(v)

    # Sort newest first (they're already in order, so reverse)
    verdicts.reverse()

    count = count_consecutive_breaches(verdicts, "fraud-detect", "reversal_rate")
    assert count == 5


def test_consecutive_breaches_stops_at_non_breach():
    from nthlayer_common.verdicts import create

    verdicts = []
    # 3 breaches, then 1 non-breach, then 2 breaches (older)
    for breach in [True, True, True, False, True, True]:
        cv = 0.08 if breach else 0.02
        v = create(
            subject={"type": "evaluation", "ref": "fraud-detect", "summary": "test"},
            judgment={"action": "flag", "confidence": 0.9},
            producer={"system": "nthlayer-measure"},
            metadata={"custom": {"slo_name": "reversal_rate", "breach": breach, "current_value": cv, "target": 0.05}},
        )
        verdicts.append(v)

    # Already in newest-first order
    count = count_consecutive_breaches(verdicts, "fraud-detect", "reversal_rate")
    assert count == 3


def test_consecutive_breaches_zero_when_no_breach():
    from nthlayer_common.verdicts import create

    v = create(
        subject={"type": "evaluation", "ref": "fraud-detect", "summary": "test"},
        judgment={"action": "approve", "confidence": 0.9},
        producer={"system": "nthlayer-measure"},
        metadata={"custom": {"slo_name": "reversal_rate", "breach": False}},
    )
    count = count_consecutive_breaches([v], "fraud-detect", "reversal_rate")
    assert count == 0


# --- evaluate_slos tests ---

@pytest.mark.asyncio
async def test_evaluate_slos_healthy_no_breach(verdict_store):
    slo = SLODefinition(
        service="fraud-detect", slo_name="reversal_rate", slo_type="judgment",
        target=0.05, window="7d", query="test_query",
    )

    with patch("nthlayer_workers.measure.adapters.prometheus.query_prometheus") as mock_query:
        mock_query.return_value = 0.02  # Under target
        results = await evaluate_slos("http://prom", [slo], verdict_store)

    assert len(results) == 1
    assert results[0].breach is False
    assert results[0].consecutive == 0


@pytest.mark.asyncio
async def test_evaluate_slos_judgment_hysteresis_not_reached(verdict_store):
    """Judgment SLO breaches but hasn't hit consecutive threshold yet."""
    slo = SLODefinition(
        service="fraud-detect", slo_name="reversal_rate", slo_type="judgment",
        target=0.05, window="7d", query="test_query",
    )

    with patch("nthlayer_workers.measure.adapters.prometheus.query_prometheus") as mock_query:
        mock_query.return_value = 0.08  # Over target
        results = await evaluate_slos("http://prom", [slo], verdict_store, hysteresis_threshold=3)

    assert len(results) == 1
    assert results[0].breach is False  # Not enough consecutive
    assert results[0].consecutive == 1


@pytest.mark.asyncio
async def test_evaluate_slos_judgment_hysteresis_reached(verdict_store):
    """Judgment SLO with enough consecutive breaches in verdict store."""
    from nthlayer_common.verdicts import create

    # Pre-populate 2 consecutive breach verdicts
    for _ in range(2):
        v = create(
            subject={"type": "evaluation", "ref": "fraud-detect", "summary": "breach"},
            judgment={"action": "flag", "confidence": 0.9},
            producer={"system": "nthlayer-measure"},
            metadata={"custom": {"slo_name": "reversal_rate", "breach": True, "current_value": 0.08, "target": 0.05}},
        )
        verdict_store.put(v)

    slo = SLODefinition(
        service="fraud-detect", slo_name="reversal_rate", slo_type="judgment",
        target=0.05, window="7d", query="test_query",
    )

    with patch("nthlayer_workers.measure.adapters.prometheus.query_prometheus") as mock_query:
        mock_query.return_value = 0.08  # Over target — this makes it 3 consecutive
        results = await evaluate_slos("http://prom", [slo], verdict_store, hysteresis_threshold=3)

    assert len(results) == 1
    assert results[0].breach is True
    assert results[0].consecutive == 3


@pytest.mark.asyncio
async def test_evaluate_slos_traditional_no_hysteresis(verdict_store):
    """Traditional SLOs breach immediately (Prometheus handles hysteresis)."""
    slo = SLODefinition(
        service="fraud-detect", slo_name="availability", slo_type="traditional",
        target=0.999, window="30d", query="test_query",
    )

    with patch("nthlayer_workers.measure.adapters.prometheus.query_prometheus") as mock_query:
        mock_query.return_value = -0.05  # Negative error budget = breach
        results = await evaluate_slos("http://prom", [slo], verdict_store)

    assert len(results) == 1
    assert results[0].breach is True


@pytest.mark.asyncio
async def test_evaluate_slos_recovery_resets_consecutive(verdict_store):
    """Value returning to healthy resets consecutive count."""
    from nthlayer_common.verdicts import create

    # Pre-populate 2 breach verdicts
    for _ in range(2):
        v = create(
            subject={"type": "evaluation", "ref": "fraud-detect", "summary": "breach"},
            judgment={"action": "flag", "confidence": 0.9},
            producer={"system": "nthlayer-measure"},
            metadata={"custom": {"slo_name": "reversal_rate", "breach": True}},
        )
        verdict_store.put(v)

    slo = SLODefinition(
        service="fraud-detect", slo_name="reversal_rate", slo_type="judgment",
        target=0.05, window="7d", query="test_query",
    )

    with patch("nthlayer_workers.measure.adapters.prometheus.query_prometheus") as mock_query:
        mock_query.return_value = 0.03  # Under target — recovery
        results = await evaluate_slos("http://prom", [slo], verdict_store)

    assert len(results) == 1
    assert results[0].breach is False
    assert results[0].consecutive == 0


@pytest.mark.asyncio
async def test_evaluate_slos_skips_missing_data(verdict_store):
    """SLOs with no Prometheus data are skipped."""
    slo = SLODefinition(
        service="fraud-detect", slo_name="reversal_rate", slo_type="judgment",
        target=0.05, window="7d", query="test_query",
    )

    with patch("nthlayer_workers.measure.adapters.prometheus.query_prometheus") as mock_query:
        mock_query.return_value = None
        results = await evaluate_slos("http://prom", [slo], verdict_store)

    assert len(results) == 0
