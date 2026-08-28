# tests/test_prometheus.py
"""Tests for the Prometheus polling adapter with mocked HTTP responses."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from nthlayer_workers.measure.adapters.prometheus import (
    SLODefinition,
    _judgment_slo_query,
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
  team: payments-ml
  tier: critical
spec:
  type: ai-gate
  slos:
    availability:
      target: 99.9
      window: 30d
    reversal_rate:
      target: 98.5
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
    slos = load_specs(specs_dir).slos
    assert len(slos) == 3
    names = {s.slo_name for s in slos}
    assert names == {"availability", "reversal_rate", "latency"}


def test_load_specs_classifies_judgment_slos(specs_dir):
    slos = load_specs(specs_dir).slos
    by_name = {s.slo_name: s for s in slos}
    assert by_name["reversal_rate"].slo_type == "judgment"
    assert by_name["availability"].slo_type == "traditional"
    assert by_name["latency"].slo_type == "traditional"


def test_load_specs_keeps_the_canonical_target_convention(specs_dir):
    """Renamed from test_load_specs_normalizes_availability_target.

    The adapter used to divide availability targets by 100, producing 0.999
    from a 99.9 spec — a second, local copy of a convention
    nthlayer-common owns (CLAUDE.md hard rule 1: targets are 0-100).
    Nothing consumed the normalised value: evaluate_slos ignores
    availability's target entirely, comparing an error-budget ratio against
    zero. Deleted with the hand-rolled reader (opensrm-fxln).
    """
    slos = load_specs(specs_dir).slos
    avail = next(s for s in slos if s.slo_name == "availability")
    assert avail.target == pytest.approx(99.9)


def test_load_specs_builds_promql(specs_dir):
    slos = load_specs(specs_dir).slos
    rev = next(s for s in slos if s.slo_name == "reversal_rate")
    assert "gen_ai_overrides_total" in rev.query
    assert "gen_ai_decisions_total" in rev.query
    assert "fraud-detect" in rev.query


def test_load_specs_empty_dir(tmp_path):
    slos = load_specs(tmp_path).slos
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
        target=95.0, window="7d", query="test_query",
    )

    with patch("nthlayer_workers.measure.adapters.prometheus.query_prometheus") as mock_query:
        mock_query.return_value = 0.02  # 2% reversed -> 98% SLI, above 95
        results = await evaluate_slos("http://prom", [slo], verdict_store)

    assert len(results) == 1
    assert results[0].breach is False
    assert results[0].consecutive == 0


@pytest.mark.asyncio
async def test_evaluate_slos_judgment_hysteresis_not_reached(verdict_store):
    """Judgment SLO breaches but hasn't hit consecutive threshold yet."""
    # target in the canonical 0-100 convention: at least 95% not reversed
    # (opensrm-fxln — these fixtures previously used a 0.05 RATIO, which
    # agreed with the dead comparison and is why it looked correct).
    slo = SLODefinition(
        service="fraud-detect", slo_name="reversal_rate", slo_type="judgment",
        target=95.0, window="7d", query="test_query",
    )

    with patch("nthlayer_workers.measure.adapters.prometheus.query_prometheus") as mock_query:
        mock_query.return_value = 0.08  # 8% reversed -> 92% SLI, under 95
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

    # target in the canonical 0-100 convention: at least 95% not reversed
    # (opensrm-fxln — these fixtures previously used a 0.05 RATIO, which
    # agreed with the dead comparison and is why it looked correct).
    slo = SLODefinition(
        service="fraud-detect", slo_name="reversal_rate", slo_type="judgment",
        target=95.0, window="7d", query="test_query",
    )

    with patch("nthlayer_workers.measure.adapters.prometheus.query_prometheus") as mock_query:
        mock_query.return_value = 0.08  # 8% reversed -> 92% SLI, under 95 — this makes it 3 consecutive
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
        target=95.0, window="7d", query="test_query",
    )

    with patch("nthlayer_workers.measure.adapters.prometheus.query_prometheus") as mock_query:
        mock_query.return_value = 0.03  # 3% reversed -> 97% SLI, recovered
        results = await evaluate_slos("http://prom", [slo], verdict_store)

    assert len(results) == 1
    assert results[0].breach is False
    assert results[0].consecutive == 0


@pytest.mark.asyncio
async def test_evaluate_slos_skips_missing_data(verdict_store):
    """SLOs with no Prometheus data are skipped."""
    slo = SLODefinition(
        service="fraud-detect", slo_name="reversal_rate", slo_type="judgment",
        target=95.0, window="7d", query="test_query",
    )

    with patch("nthlayer_workers.measure.adapters.prometheus.query_prometheus") as mock_query:
        mock_query.return_value = None
        results = await evaluate_slos("http://prom", [slo], verdict_store)

    assert len(results) == 0


# --- opensrm-fxln: judgment SLO targets use the 0-100 convention ---


@pytest.mark.asyncio
async def test_judgment_slo_breaches_when_reversal_exceeds_target(verdict_store):
    """fraud-detect declares `reversal_rate: target: 98.5` — "at least 98.5% of
    decisions must not be reversed" (nthlayer-common CLAUDE.md hard rule 1,
    opensrm-5fff.1). The query returns a reversal RATIO.

    The adapter compared the raw ratio against the 0-100 target:

        0.05 > 98.5  ->  False, always

    so the SLO could never breach. measure/worker.py:240 already does this
    correctly — `current_pct = current_value * 100`, healthy when
    `current_pct >= target` — so the adapter was the outlier, disagreeing
    with its own sibling and with the spec.

    Here 4% reversed means a 96% non-reversal SLI against a 98.5 target:
    a breach.
    """
    slo = SLODefinition(
        service="fraud-detect", slo_name="reversal_rate", slo_type="judgment",
        target=98.5, window="7d", query="test_query",
    )

    with patch("nthlayer_workers.measure.adapters.prometheus.query_prometheus") as mock_query:
        mock_query.return_value = 0.04  # 4% reversed -> 96% SLI, under 98.5
        results = await evaluate_slos("http://prom", [slo], verdict_store)

    assert results[0].consecutive == 1, "a 96% SLI against a 98.5 target is a breach"


@pytest.mark.asyncio
async def test_judgment_slo_healthy_when_reversal_within_target(verdict_store):
    """The other side: 1% reversed is a 99% SLI, comfortably above 98.5.

    Without this, a fix that simply inverted the comparison would pass the
    breach test above while breaching on everything.
    """
    slo = SLODefinition(
        service="fraud-detect", slo_name="reversal_rate", slo_type="judgment",
        target=98.5, window="7d", query="test_query",
    )

    with patch("nthlayer_workers.measure.adapters.prometheus.query_prometheus") as mock_query:
        mock_query.return_value = 0.01  # 1% reversed -> 99% SLI
        results = await evaluate_slos("http://prom", [slo], verdict_store)

    assert results[0].breach is False
    assert results[0].consecutive == 0


# --- opensrm-fxln: load_specs must understand v2, .yml, and count failures ---


def _v1(name: str) -> str:
    return (
        "apiVersion: srm/v1\nkind: ServiceReliabilityManifest\n"
        f"metadata: {{name: {name}, team: t, tier: critical}}\n"
        "spec:\n  type: api\n  slos:\n    availability:\n"
        "      target: 99.9\n      window: 30d\n"
        '      indicator: {query: \'up{job="x"}\'}\n'
    )


def _v2(name: str) -> str:
    return (
        "apiVersion: opensrm.nthlayer.io/v2\nkind: ServiceManifest\n"
        f"metadata: {{name: {name}, labels: {{tier: critical}}}}\n"
        "spec:\n  owner: {group: 'group:default/t'}\n"
        f"  service: {{name: {name}, type: api}}\n"
        "  slo:\n    - apiVersion: openslo/v1\n      kind: SLO\n"
        "      metadata: {name: availability}\n"
        f"      spec:\n        service: {name}\n"
        "        objectives: [{target: 0.999}]\n"
    )


class TestLoadSpecsUnderstandsBothFormats:
    """opensrm-fxln — the adapter hand-rolled v1 parsing and silently
    ignored v2.

    It read `spec.slos`, which exists only in srm/v1. A v2 manifest carries
    `spec.slo` and `spec.judgment_slo`, so slo_defs came back empty and the
    service contributed ZERO SLOs — no error, no warning, exit 0. The whole
    ecosystem migrated to v2 under opensrm-ih0v, so anything migrated was
    silently unmeasured.
    """

    def test_v2_manifest_yields_slos(self, tmp_path):
        (tmp_path / "svc.yaml").write_text(_v2("checkout"))

        loaded = load_specs(tmp_path)

        assert [s.service for s in loaded.slos] == ["checkout"]

    def test_v1_and_v2_in_one_directory_both_load(self, tmp_path):
        (tmp_path / "old.yaml").write_text(_v1("legacy"))
        (tmp_path / "new.yaml").write_text(_v2("checkout"))

        loaded = load_specs(tmp_path)

        assert {s.service for s in loaded.slos} == {"legacy", "checkout"}

    def test_yml_suffix_is_seen(self, tmp_path):
        """`.glob("*.yaml")` missed `.yml` — the same silent subset reached
        by file extension rather than parse error (opensrm-oh27)."""
        (tmp_path / "svc.yml").write_text(_v1("legacy"))

        assert [s.service for s in load_specs(tmp_path).slos] == ["legacy"]

    def test_broken_manifest_is_counted_not_swallowed(self, tmp_path):
        (tmp_path / "good.yaml").write_text(_v1("legacy"))
        (tmp_path / "bad.yaml").write_text(
            "apiVersion: srm/v1\nkind: ServiceReliabilityManifest\n"
            "metadata: {name: b, team: t, tier: nonexistent-tier}\n"
            "spec: {type: api, slos: {}}\n"
        )

        loaded = load_specs(tmp_path)

        assert [s.service for s in loaded.slos] == ["legacy"]
        assert loaded.parse_failures == 1

    def test_foreign_yaml_is_not_counted(self, tmp_path):
        (tmp_path / "prometheus.yaml").write_text("groups:\n  - name: g\n    rules: []\n")
        (tmp_path / "svc.yaml").write_text(_v1("legacy"))

        loaded = load_specs(tmp_path)

        assert loaded.parse_failures == 0

    def test_manifest_without_a_name_is_not_named_after_its_file(self, tmp_path):
        """`metadata.get("name", spec_file.stem)` attributed a nameless
        manifest's SLOs to a service named after the file — measuring them
        against the wrong service rather than not at all."""
        (tmp_path / "checkout.yaml").write_text(
            "apiVersion: srm/v1\nkind: ServiceReliabilityManifest\n"
            "metadata: {team: t, tier: critical}\n"
            "spec: {type: api, slos: {availability: {target: 99.9}}}\n"
        )

        loaded = load_specs(tmp_path)

        assert [s.service for s in loaded.slos] == []
        assert loaded.parse_failures == 1
