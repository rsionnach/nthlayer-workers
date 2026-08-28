# tests/test_prometheus.py
"""Tests for the Prometheus polling adapter with mocked HTTP responses."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from nthlayer_common.manifest.models import SLODefinition as ManifestSLO

from nthlayer_workers.measure.adapters.prometheus import (
    _JUDGMENT_SLO_QUERIES,
    EvaluationResult,
    SLODefinition,
    _query_for,
    count_consecutive_breaches,
    evaluate_slos,
    evaluation_custom_metadata,
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
    """The adapter used to divide availability targets by 100, producing 0.999
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


# --- the judgment PromQL builders (opensrm-y7dd: lookup-dict refactor) ---
# Exercised directly: the _judgment_slo_query wrapper was deleted in
# opensrm-fxln once _query_for read the dict itself, and a second accessor
# that disagreed with the live one about unknown types was worse than none.


def test_judgment_query_builder_reversal_rate():
    q = _JUDGMENT_SLO_QUERIES["reversal_rate"]("fraud-detect", "5m")
    assert "gen_ai_overrides_total" in q
    assert 'service="fraud-detect"' in q
    assert "[5m]" in q


def test_judgment_query_builder_high_confidence_failure():
    q = _JUDGMENT_SLO_QUERIES["high_confidence_failure"]("svc-a", "10m")
    assert "gen_ai_overrides_hcf_total" in q
    assert 'confidence_bucket="high"' in q


def test_judgment_query_builder_calibration_window_agnostic():
    # Calibration is a gauge: window argument is intentionally ignored.
    q5 = _JUDGMENT_SLO_QUERIES["calibration"]("svc", "5m")
    q1h = _JUDGMENT_SLO_QUERIES["calibration"]("svc", "1h")
    assert q5 == q1h
    assert q5 == 'gen_ai_calibration_error{service="svc"}'


def test_unbuilt_judgment_type_falls_back_to_recording_rule():
    # Four of the eight JUDGMENT_SLO_TYPES have no builder above. They must
    # reach the recording-rule convention, NOT the empty string the deleted
    # wrapper returned: Prometheus 400s on an empty query, and
    # query_prometheus reads that failure as no-data and skips the SLO —
    # unbreachable, silently (opensrm-fxln).
    slo = ManifestSLO(
        name="drift-watch",
        target=98.0,
        slo_type="judgment",
        window="5m",
        judgment_type="segment_disparity",
    )
    assert _query_for("svc", slo)[0] == 'slo:drift-watch:ratio{service="svc"}'


def test_judgment_type_not_slo_name_selects_the_builder():
    # In v2 metadata.name is author-chosen and independent of
    # spec.judgment_type. An SLO named anything must still get the builder
    # its TYPE names.
    slo = ManifestSLO(
        name="model-quality",
        target=98.5,
        slo_type="judgment",
        window="5m",
        judgment_type="reversal_rate",
    )
    assert "gen_ai_overrides_total" in _query_for("fraud-detect", slo)[0]


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
            metadata={"custom": evaluation_custom_metadata(EvaluationResult(
                service="fraud-detect", slo_name="reversal_rate",
                slo_type="judgment", target=95.0, current_value=0.08,
                breach=False, consecutive=0, raw_breach=True,
            ))},
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
    for raw_breach in [True, True, True, False, True, True]:
        v = create(
            subject={"type": "evaluation", "ref": "fraud-detect", "summary": "test"},
            judgment={"action": "flag", "confidence": 0.9},
            producer={"system": "nthlayer-measure"},
            metadata={"custom": evaluation_custom_metadata(EvaluationResult(
                service="fraud-detect", slo_name="reversal_rate",
                slo_type="judgment", target=95.0,
                current_value=0.08 if raw_breach else 0.02,
                breach=False, consecutive=0, raw_breach=raw_breach,
            ))},
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
        query_kind="judgment_rate",
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
        query_kind="judgment_rate",
    )

    with patch("nthlayer_workers.measure.adapters.prometheus.query_prometheus") as mock_query:
        mock_query.return_value = 0.08  # 8% reversed -> 92% SLI, under 95
        results = await evaluate_slos("http://prom", [slo], verdict_store, hysteresis_threshold=3)

    assert len(results) == 1
    assert results[0].breach is False  # Not enough consecutive
    assert results[0].consecutive == 1


# test_evaluate_slos_judgment_hysteresis_reached was deleted in opensrm-fxln.
# It hand-built its prior-window verdicts with `target: 0.05` — a 0-1 ratio
# that cmd_evaluate_once never writes, since it stores the manifest's 0-100
# target. That unreal shape was the only thing that satisfied the counter's
# `current > target`, so the test passed while the real pipeline could not
# reach the threshold at all. Superseded by
# test_judgment_hysteresis_reaches_threshold_over_real_verdicts, which seeds
# through evaluation_custom_metadata.


@pytest.mark.asyncio
async def test_evaluate_slos_traditional_no_hysteresis(verdict_store):
    """Traditional SLOs breach immediately (Prometheus handles hysteresis)."""
    slo = SLODefinition(
        service="fraud-detect", slo_name="availability", slo_type="traditional",
        target=0.999, window="30d", query="test_query",
        query_kind="error_budget",
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
            # No raw_breach: a verdict written before opensrm-fxln. The
            # counter stops on these rather than guessing their per-window
            # state, so history reads as 0 and recovery is unambiguous.
            metadata={"custom": {"slo_name": "reversal_rate", "breach": True}},
        )
        verdict_store.put(v)

    slo = SLODefinition(
        service="fraud-detect", slo_name="reversal_rate", slo_type="judgment",
        target=95.0, window="7d", query="test_query",
        query_kind="judgment_rate",
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
        query_kind="judgment_rate",
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
        query_kind="judgment_rate",
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
        query_kind="judgment_rate",
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


class TestQueryAndBreachLogicAreAMatchedPair:
    """opensrm-fxln R5 correctness — the adapter's breach branches are
    hard-coded to the semantics of its OWN synthesised queries.

    `availability` breaches on `current < 0.0`, which is only meaningful for
    the synthesised `slo:error_budget:ratio`. Judgment SLOs invert
    (`(1-rate)*100`), which is only right for the synthesised
    overrides/decisions RATIO — measure/worker.py does not invert because it
    feeds get_sli_value(indicator_query), whose value is already an SLI.

    So preferring a manifest's indicator_query over the synthesised one
    silently breaks both: availability compares a plain ratio against zero
    and can never breach, and a judgment SLI gets inverted twice.
    """

    def test_availability_uses_the_synthesised_error_budget_query(self, tmp_path):
        """Even though this manifest declares its own indicator query."""
        (tmp_path / "svc.yaml").write_text(
            "apiVersion: srm/v1\nkind: ServiceReliabilityManifest\n"
            "metadata: {name: svc, team: t, tier: critical}\n"
            "spec:\n  type: api\n  slos:\n    availability:\n"
            "      target: 99.9\n      window: 30d\n"
            "      indicator: {query: 'up{job=\"x\"}'}\n"
        )

        slo = load_specs(tmp_path).slos[0]

        assert "slo:error_budget:ratio" in slo.query, (
            "availability's breach check is `current < 0.0`, which only the "
            "synthesised error-budget query can satisfy"
        )

    def test_judgment_slo_is_classified_by_type_not_by_name(self, tmp_path):
        """In v2, metadata.name and spec.judgment_type are independent.

        Dispatching the breach check on the NAME sends a judgment SLO called
        anything else to the classical `current < target` branch — and with a
        0-100 target against a 0-1 ratio that is always a breach, then
        hysteresis turns it into a real one. The exact inverse of the bug
        this bead fixed.
        """
        (tmp_path / "svc.yaml").write_text(
            "apiVersion: opensrm.nthlayer.io/v2\nkind: ServiceManifest\n"
            "metadata: {name: svc, labels: {tier: critical}}\n"
            "spec:\n  owner: {group: 'group:default/t'}\n"
            "  service: {name: svc, type: ai-gate}\n"
            "  judgment_slo:\n    - metadata: {name: reversal-guard}\n"
            "      spec:\n        service: svc\n"
            "        judgment_type: reversal_rate\n"
            "        target: {maximum_reversal_rate: 0.05}\n"
        )

        slo = load_specs(tmp_path).slos[0]

        assert slo.slo_name == "reversal-guard"
        assert slo.judgment_type == "reversal_rate", (
            "the breach check must dispatch on judgment_type; the NAME is "
            "author-chosen and independent of it in v2"
        )

    def test_a_judgment_type_with_no_builder_gets_the_recording_rule_query(self, tmp_path):
        """The same rule as
        test_unbuilt_judgment_type_falls_back_to_recording_rule, which
        carries the rationale — reached through load_specs rather than by
        calling _query_for directly.
        """
        (tmp_path / "svc.yaml").write_text(
            "apiVersion: opensrm.nthlayer.io/v2\nkind: ServiceManifest\n"
            "metadata: {name: svc, labels: {tier: critical}}\n"
            "spec:\n  owner: {group: 'group:default/t'}\n"
            "  service: {name: svc, type: ai-gate}\n"
            "  judgment_slo:\n    - metadata: {name: seg}\n"
            "      spec:\n        service: svc\n"
            "        judgment_type: segments\n"
            "        target: {maximum_variance_from_overall: 0.15}\n"
        )

        for slo in load_specs(tmp_path).slos:
            assert slo.query, f"{slo.slo_name} got an empty query"


@pytest.mark.asyncio
async def test_breach_dispatches_on_judgment_type_not_name(verdict_store):
    """A judgment SLO whose author named it something other than its type.

    Dispatching on slo_name sent this to the classical `current < target`
    branch: 0.04 < 98.5 is always true, so it breached every window and
    hysteresis turned that into a real breach after three — the inverse of
    the bug this bead fixed, on the path v2 manifests actually take.
    """
    slo = SLODefinition(
        service="fraud-detect", slo_name="reversal-guard", slo_type="judgment",
        target=98.5, window="7d", query="test_query",
        query_kind="judgment_rate", judgment_type="reversal_rate",
    )

    with patch("nthlayer_workers.measure.adapters.prometheus.query_prometheus") as mock_query:
        mock_query.return_value = 0.01  # 1% reversed -> 99% SLI, healthy
        results = await evaluate_slos("http://prom", [slo], verdict_store)

    assert results[0].breach is False
    assert results[0].consecutive == 0, "a healthy judgment SLO must not breach"


# --- opensrm-fxln edge-cases pass: the breach rule must travel with the query ---


def _seed_verdicts(store, results, n=1):
    """Seed prior-window verdicts the way cmd_evaluate_once actually writes them.

    Goes through evaluation_custom_metadata, the single definition shared by
    the CLI writer and count_consecutive_breaches. A hand-built dict here is
    exactly the fixture-provenance trap this pass caught: the old hysteresis
    fixture seeded `target: 0.05`, a ratio shape the pipeline never produces,
    so it agreed with a comparison that could not fire.
    """
    from nthlayer_common.verdicts import create

    from nthlayer_workers.measure.adapters.prometheus import (
        evaluation_custom_metadata,
    )
    for _ in range(n):
        for r in results:
            store.put(create(
                subject={"type": "evaluation", "ref": r.service, "summary": "w"},
                judgment={"action": "flag", "confidence": 0.9},
                producer={"system": "nthlayer-measure"},
                metadata={"custom": evaluation_custom_metadata(r)},
            ))


@pytest.mark.asyncio
async def test_judgment_hysteresis_reaches_threshold_over_real_verdicts(verdict_store):
    """The bead's headline defect, end-to-end rather than per-window.

    The per-window raw_breach was fixed, but count_consecutive_breaches
    re-derived breach as `current_value > target` — a raw 0-1 rate against a
    0-100 target, so 0.08 > 95.0 was never true. History always returned 0,
    consecutive capped at 1, and `breach = consecutive >= 3` stayed
    unreachable. The fix could not fire through the pipeline it exists for.
    """
    slo = SLODefinition(
        service="fraud-detect", slo_name="reversal-guard", slo_type="judgment",
        judgment_type="reversal_rate", target=95.0, window="7d",
        query="q", query_kind="judgment_rate",
    )

    with patch(
        "nthlayer_workers.measure.adapters.prometheus.query_prometheus"
    ) as mock_query:
        mock_query.return_value = 0.08  # 8% reversed -> 92% SLI, under 95
        first = await evaluate_slos("http://prom", [slo], verdict_store,
                                    hysteresis_threshold=3)
        _seed_verdicts(verdict_store, first)
        second = await evaluate_slos("http://prom", [slo], verdict_store,
                                     hysteresis_threshold=3)
        _seed_verdicts(verdict_store, second)
        third = await evaluate_slos("http://prom", [slo], verdict_store,
                                    hysteresis_threshold=3)

    assert [r.consecutive for r in (first[0], second[0], third[0])] == [1, 2, 3]
    assert first[0].breach is False
    assert third[0].breach is True


@pytest.mark.asyncio
async def test_recording_rule_fallback_is_not_inverted(verdict_store):
    """A builder-less judgment type gets an SLI query, so it must not invert.

    Four of the eight JUDGMENT_SLO_TYPES fall through to `slo:{name}:ratio`,
    which is a GOOD-ratio SLI by convention, not an overrides/decisions rate.
    Inverting it read a healthy 0.99 as 1.0% and breached every window, while
    a genuinely bad 0.01 read as 99% healthy — verdicts exactly backwards for
    half the taxonomy.
    """
    healthy = SLODefinition(
        service="svc", slo_name="seg", slo_type="judgment",
        judgment_type="segment_disparity", target=95.0, window="7d",
        query="q", query_kind="ratio",
    )

    with patch(
        "nthlayer_workers.measure.adapters.prometheus.query_prometheus"
    ) as mock_query:
        mock_query.return_value = 0.99  # 99% good — comfortably above 95
        results = await evaluate_slos("http://prom", [healthy], verdict_store)

    assert results[0].raw_breach is False, (
        "0.99 is a 99% SLI against a 95 target; inverting it makes it 1.0%"
    )


@pytest.mark.asyncio
async def test_latency_rule_follows_the_query_not_the_slo_name(verdict_store):
    """Same name-vs-type gap the judgment branch closed, on the classical side.

    The latency comparison keyed on `slo_name == "latency"`. A v2 SLO named
    anything else — p99-latency, checkout-latency — fell to the ratio branch
    and had its seconds compared against a 0-100 target.
    """
    slo = SLODefinition(
        service="svc", slo_name="p99-latency", slo_type="traditional",
        target=100.0, window="30d", query="q", query_kind="latency_seconds",
    )

    with patch(
        "nthlayer_workers.measure.adapters.prometheus.query_prometheus"
    ) as mock_query:
        mock_query.return_value = 0.05  # 50ms, well inside a 100ms target
        results = await evaluate_slos("http://prom", [slo], verdict_store)

    assert results[0].raw_breach is False


def test_query_for_reports_the_breach_rule_with_the_query():
    """_query_for's invariant, as a returned value rather than a comment."""
    rate = ManifestSLO(name="rev", target=98.5, slo_type="judgment",
                       window="5m", judgment_type="reversal_rate")
    unbuilt = ManifestSLO(name="seg", target=98.0, slo_type="judgment",
                          window="5m", judgment_type="segment_disparity")
    latency = ManifestSLO(name="latency", target=100.0, slo_type="traditional",
                          window="30d")
    avail = ManifestSLO(name="availability", target=99.9, slo_type="traditional",
                        window="30d")

    assert _query_for("s", rate)[1] == "judgment_rate"
    assert _query_for("s", unbuilt)[1] == "ratio"
    assert _query_for("s", latency)[1] == "latency_seconds"
    assert _query_for("s", avail)[1] == "error_budget"


@pytest.mark.asyncio
async def test_hysteresis_survives_a_realistic_number_of_slos(verdict_store):
    """History was fetched with a flat limit=20 across ALL services and SLOs.

    One cycle over 20+ SLOs writes 20+ verdicts, so the next cycle's window
    held at most the newest window per SLO — consecutive capped at 1 and the
    threshold was unreachable again, this time by pagination rather than
    arithmetic. Newly reachable precisely because load_specs went from
    yielding zero SLOs on a v2 manifest to yielding all of them.
    """
    slos = [
        SLODefinition(
            service=svc, slo_name=f"guard-{i}", slo_type="judgment",
            judgment_type="reversal_rate", target=95.0, window="7d",
            query="q", query_kind="judgment_rate",
        )
        for svc in ("svc-a", "svc-b")
        for i in range(12)  # 24 SLOs — more than the old flat limit
    ]

    with patch(
        "nthlayer_workers.measure.adapters.prometheus.query_prometheus"
    ) as mock_query:
        mock_query.return_value = 0.08  # 92% SLI against a 95 target
        for _ in range(2):
            _seed_verdicts(
                verdict_store,
                await evaluate_slos("http://prom", slos, verdict_store,
                                    hysteresis_threshold=3),
            )
        final = await evaluate_slos("http://prom", slos, verdict_store,
                                    hysteresis_threshold=3)

    assert {r.consecutive for r in final} == {3}
    assert all(r.breach for r in final)


@pytest.mark.asyncio
async def test_latency_target_is_read_in_its_declared_unit(tmp_path, verdict_store):
    """load_specs -> evaluate_slos for latency, the round trip nothing covered.

    The branch hard-coded `target / 1000.0`, assuming milliseconds. The
    target reaches it from the parser, which preserves whatever `unit` the
    manifest declared — so a seconds-denominated SLO was compared against a
    threshold a thousand times too small and breached every window.
    """
    (tmp_path / "svc.yaml").write_text(
        "apiVersion: srm/v1\nkind: ServiceReliabilityManifest\n"
        "metadata: {name: svc, team: t, tier: critical}\n"
        "spec:\n  type: api\n  slos:\n"
        "    latency: {target: 2, unit: s, percentile: p99, window: 30d}\n"
    )
    slo = next(s for s in load_specs(tmp_path).slos if s.slo_name == "latency")

    with patch(
        "nthlayer_workers.measure.adapters.prometheus.query_prometheus"
    ) as mock_query:
        mock_query.return_value = 1.5  # 1.5s, inside a 2s target
        results = await evaluate_slos("http://prom", [slo], verdict_store)

    assert results[0].raw_breach is False, (
        "a 2s target read as 2ms makes every response a breach"
    )


@pytest.mark.xfail(
    strict=True,
    reason="opensrm-vrpa: feedback_latency is in opensrm's v1 schema.json "
    "but not in nthlayer-common's JUDGMENT_SLO_TYPES, so parser/v1.py never "
    "sets judgment_type for it. Flips to a pass — and so fails strictly — "
    "the day the vocabularies are reconciled, which is the signal to "
    "reconnect the builder here.",
)
def test_feedback_latency_cannot_reach_its_own_branch(tmp_path):
    """Pins a schema-vs-parser divergence, not a decision made in this repo.

    Until it is fixed, a v1 `slos.feedback_latency` is evaluated by the
    "ratio" rule — `current_value * 100 < target` against a seconds gauge,
    so 0.5s reads as 50. The builder and judgment_duration branch that would
    read it correctly exist and cannot be selected.
    """
    (tmp_path / "svc.yaml").write_text(
        "apiVersion: srm/v1\nkind: ServiceReliabilityManifest\n"
        "metadata: {name: svc, team: t, tier: critical}\n"
        "spec:\n  type: ai-gate\n  slos:\n"
        "    feedback_latency: {target: 300, unit: s, window: 7d}\n"
    )
    slo = next(
        s for s in load_specs(tmp_path).slos if s.slo_name == "feedback_latency"
    )
    assert slo.query_kind == "judgment_duration"


@pytest.mark.parametrize(
    ("unit", "expected_seconds"),
    [("s", 2.0), ("S", 2.0), ("sec", 2.0), ("seconds", 2.0), (" ms ", 0.002)],
)
def test_duration_units_are_matched_leniently(unit, expected_seconds):
    """Manifests are hand-written; `unit: seconds` must not mean milliseconds.

    An unmatched unit assumes ms, so every one of these spellings falling
    through would give a threshold 1000x too small and breach every window.
    """
    from nthlayer_workers.measure.adapters.prometheus import _target_seconds

    assert _target_seconds(2.0, unit) == pytest.approx(expected_seconds)


@pytest.mark.parametrize("value", [0, -1])
def test_evaluate_once_rejects_a_hysteresis_below_one(tmp_path, value, capsys):
    """`consecutive >= 0` is true before any window runs.

    --hysteresis 0 makes every judgment SLO breach on its first evaluation —
    the exact false-positive inverse of the bug this bead fixed — and a
    negative value drives the history window to zero.
    """
    import argparse

    from nthlayer_workers.measure.cli import cmd_evaluate_once

    args = argparse.Namespace(
        specs_dir=str(tmp_path), hysteresis=value,
        verdict_store=str(tmp_path / "v.db"), prometheus_url="http://prom",
        decision_store=None,
    )
    with pytest.raises(SystemExit) as exc:
        cmd_evaluate_once(args)

    assert exc.value.code == 2


def test_a_non_string_unit_warns_rather_than_aborting_the_cycle():
    """`unit: 100` reaches here as an int; the parser does not validate it.

    Raising would propagate out of evaluate_slos' per-SLO loop and end the
    run with verdicts already written for earlier SLOs — a partial cycle,
    which is worse than a wrong-but-flagged threshold.
    """
    from nthlayer_workers.measure.adapters.prometheus import _target_seconds

    assert _target_seconds(2.0, 100) == pytest.approx(0.002)  # assumes ms
