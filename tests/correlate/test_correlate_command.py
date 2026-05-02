# tests/test_correlate_command.py
"""Tests for the correlate CLI subcommand with mocked Prometheus."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from nthlayer_workers.correlate.prometheus import (
    blast_radius_services,
    load_dependency_graph,
    verdict_to_event,
)
from nthlayer_workers.correlate.types import EventType


# --- Fixtures ---

FRAUD_SPEC = """\
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
  dependencies:
    - name: feature-store
      type: database
      critical: true
"""

PAYMENT_SPEC = """\
apiVersion: srm/v1
kind: ServiceReliabilityManifest
metadata:
  name: payment-api
  tier: critical
spec:
  type: api
  slos:
    availability:
      target: 99.99
      window: 30d
  dependencies:
    - name: fraud-detect
      type: api
      critical: true
"""


@pytest.fixture
def specs_dir(tmp_path):
    (tmp_path / "fraud-detect.yaml").write_text(FRAUD_SPEC)
    (tmp_path / "payment-api.yaml").write_text(PAYMENT_SPEC)
    return tmp_path


# --- load_dependency_graph tests ---

def test_load_dependency_graph(specs_dir):
    graph = load_dependency_graph(str(specs_dir))
    assert "fraud-detect" in graph
    assert "payment-api" in graph
    assert "feature-store" in graph["fraud-detect"]["dependencies"]
    # Reverse dependency: payment-api depends on fraud-detect
    assert "payment-api" in graph["fraud-detect"]["dependents"]


def test_load_dependency_graph_empty(tmp_path):
    graph = load_dependency_graph(str(tmp_path))
    assert graph == {}


# --- blast_radius_services tests ---

def test_blast_radius_includes_trigger_and_dependents(specs_dir):
    graph = load_dependency_graph(str(specs_dir))
    affected = blast_radius_services("fraud-detect", graph)
    assert "fraud-detect" in affected
    assert "payment-api" in affected  # depends on fraud-detect
    assert "feature-store" in affected  # fraud-detect depends on it


def test_blast_radius_leaf_service(specs_dir):
    graph = load_dependency_graph(str(specs_dir))
    affected = blast_radius_services("payment-api", graph)
    assert "payment-api" in affected
    assert "fraud-detect" in affected  # payment-api depends on it


# --- verdict_to_event tests ---

def test_verdict_to_event():
    from nthlayer_common.verdicts import create

    v = create(
        subject={"type": "evaluation", "ref": "fraud-detect", "summary": "breach"},
        judgment={"action": "flag", "confidence": 0.85},
        producer={"system": "nthlayer-measure"},
        metadata={"custom": {"slo_name": "reversal_rate", "breach": True}},
    )
    event = verdict_to_event(v)
    assert event.type == EventType.VERDICT
    assert event.service == "fraud-detect"
    assert event.payload["verdict_id"] == v.id
    assert event.payload["breach"] is True


# --- correlate_command integration test ---

def test_correlate_command_writes_correlation_verdict(specs_dir, tmp_path):
    """Full correlate command with mocked Prometheus returns correlation verdict."""
    from nthlayer_common.verdicts import SQLiteVerdictStore, create as verdict_create

    # Set up verdict store with a trigger evaluation verdict
    store_path = str(tmp_path / "verdicts.db")
    store = SQLiteVerdictStore(store_path)
    trigger = verdict_create(
        subject={"type": "evaluation", "ref": "fraud-detect", "summary": "reversal rate breach"},
        judgment={"action": "flag", "confidence": 0.9},
        producer={"system": "nthlayer-measure"},
        metadata={"custom": {
            "slo_type": "judgment",
            "slo_name": "reversal_rate",
            "breach": True,
            "consecutive": 3,
        }},
    )
    store.put(trigger)

    # Mock Prometheus to return some alerts and metric breaches
    async def mock_fetch_alerts(client, url, services):
        from nthlayer_workers.correlate.types import SitRepEvent, EventType
        return [SitRepEvent(
            id="alert-1",
            timestamp="2026-03-25T10:00:00Z",
            source="prometheus",
            type=EventType.ALERT,
            service="fraud-detect",
            environment="production",
            severity=0.8,
            payload={"alert_name": "FraudDetectHighErrorRate"},
        )]

    async def mock_fetch_breaches(client, url, services, window_minutes=30):
        from nthlayer_workers.correlate.types import SitRepEvent, EventType
        return [SitRepEvent(
            id="breach-1",
            timestamp="2026-03-25T10:01:00Z",
            source="prometheus",
            type=EventType.METRIC_BREACH,
            service="fraud-detect",
            environment="production",
            severity=0.7,
            payload={"metric": "slo:error_budget:ratio", "value": -0.05},
        )]

    from nthlayer_workers.correlate.cli import correlate_command

    with patch("nthlayer_workers.correlate.prometheus.fetch_alerts", side_effect=mock_fetch_alerts), \
         patch("nthlayer_workers.correlate.prometheus.fetch_metric_breaches", side_effect=mock_fetch_breaches):
        # Need to also patch the import inside the function
        result = correlate_command(
            trigger_verdict_id=trigger.id,
            prometheus_url="http://mock:9090",
            specs_dir=str(specs_dir),
            verdict_store_path=store_path,
        )

    assert result == 0

    # Check that a correlation verdict was written
    from nthlayer_common.verdicts import VerdictFilter
    corr_verdicts = store.query(VerdictFilter(subject_type="correlation", limit=10))
    assert len(corr_verdicts) >= 1
    cv = corr_verdicts[0]
    assert cv.subject.ref == "fraud-detect"
    assert cv.producer.system == "nthlayer-correlate"
    assert trigger.id in cv.lineage.context


def test_correlate_command_missing_verdict(tmp_path):
    """Returns error code 1 when trigger verdict doesn't exist."""
    from nthlayer_common.verdicts import SQLiteVerdictStore

    store_path = str(tmp_path / "verdicts.db")
    SQLiteVerdictStore(store_path)  # create empty store

    from nthlayer_workers.correlate.cli import correlate_command

    result = correlate_command(
        trigger_verdict_id="vrd-nonexistent",
        prometheus_url="http://mock:9090",
        specs_dir=str(tmp_path),
        verdict_store_path=store_path,
    )
    assert result == 1


# ---------------------------------------------------------------------------
# Task 5: Trace backend integration tests
# ---------------------------------------------------------------------------

from datetime import datetime, timezone  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

from nthlayer_workers.correlate.traces.protocol import (  # noqa: E402
    ServiceTraceProfile,
    TraceEvidence,
)


def _make_trace_evidence() -> TraceEvidence:
    """Build a minimal TraceEvidence fixture."""
    now = datetime.now(tz=timezone.utc)
    return TraceEvidence(
        services=[ServiceTraceProfile(
            service="fraud-detect",
            time_window_start=now,
            time_window_end=now,
            callers=[], callees=[],
            p50_latency_ms=50.0, p95_latency_ms=120.0, p99_latency_ms=340.0,
            baseline_p50_ms=18.0, latency_change_pct=178.0,
            error_rate=0.02, error_count=24, total_request_count=1204,
            top_errors=[], slow_operations=[],
            sample_error_traces=[], sample_slow_traces=[],
        )],
        topology_divergence=None,
        query_time_ms=6240.0,
        backend="tempo",
    )


def _setup_trigger_and_mocks(specs_dir, tmp_path):
    """Create a trigger verdict and return (store_path, trigger_id, mock_alerts, mock_breaches)."""
    from nthlayer_common.verdicts import SQLiteVerdictStore, create as verdict_create
    from nthlayer_workers.correlate.types import SitRepEvent, EventType

    store_path = str(tmp_path / "verdicts.db")
    store = SQLiteVerdictStore(store_path)
    trigger = verdict_create(
        subject={"type": "evaluation", "ref": "fraud-detect", "summary": "breach"},
        judgment={"action": "flag", "confidence": 0.9},
        producer={"system": "nthlayer-measure"},
        metadata={"custom": {"slo_name": "reversal_rate", "breach": True}},
    )
    store.put(trigger)

    async def mock_fetch_alerts(client, url, services):
        return [SitRepEvent(
            id="alert-1", timestamp="2026-03-25T10:00:00Z",
            source="prometheus", type=EventType.ALERT,
            service="fraud-detect", environment="production",
            severity=0.8, payload={"alert_name": "HighErrorRate"},
        )]

    async def mock_fetch_breaches(client, url, services, window_minutes=30):
        return [SitRepEvent(
            id="breach-1", timestamp="2026-03-25T10:01:00Z",
            source="prometheus", type=EventType.METRIC_BREACH,
            service="fraud-detect", environment="production",
            severity=0.7, payload={"metric": "slo:error_budget:ratio", "value": -0.05},
        )]

    return store_path, trigger.id, mock_fetch_alerts, mock_fetch_breaches


def test_correlate_with_trace_backend(specs_dir, tmp_path):
    """Trace evidence metadata appears in correlation verdict."""
    from nthlayer_workers.correlate.cli import correlate_command

    store_path, trigger_id, mock_alerts, mock_breaches = _setup_trigger_and_mocks(specs_dir, tmp_path)

    mock_backend = AsyncMock()
    mock_backend.get_trace_evidence.return_value = _make_trace_evidence()

    with patch("nthlayer_workers.correlate.prometheus.fetch_alerts", side_effect=mock_alerts), \
         patch("nthlayer_workers.correlate.prometheus.fetch_metric_breaches", side_effect=mock_breaches):
        result = correlate_command(
            trigger_verdict_id=trigger_id,
            prometheus_url="http://mock:9090",
            specs_dir=str(specs_dir),
            verdict_store_path=store_path,
            trace_backend=mock_backend,
        )

    assert result == 0

    from nthlayer_common.verdicts import SQLiteVerdictStore, VerdictFilter
    store = SQLiteVerdictStore(store_path)
    corr = store.query(VerdictFilter(subject_type="correlation", limit=1))[0]
    custom = corr.metadata.custom
    assert custom["evidence_sources"]["trace_backend"] == "tempo"
    assert custom["trace_query_time_ms"] == pytest.approx(6240.0)


def test_correlate_without_trace_backend(specs_dir, tmp_path):
    """Existing behavior: no trace metadata when no backend configured."""
    from nthlayer_workers.correlate.cli import correlate_command

    store_path, trigger_id, mock_alerts, mock_breaches = _setup_trigger_and_mocks(specs_dir, tmp_path)

    with patch("nthlayer_workers.correlate.prometheus.fetch_alerts", side_effect=mock_alerts), \
         patch("nthlayer_workers.correlate.prometheus.fetch_metric_breaches", side_effect=mock_breaches):
        result = correlate_command(
            trigger_verdict_id=trigger_id,
            prometheus_url="http://mock:9090",
            specs_dir=str(specs_dir),
            verdict_store_path=store_path,
        )

    assert result == 0

    from nthlayer_common.verdicts import SQLiteVerdictStore, VerdictFilter
    store = SQLiteVerdictStore(store_path)
    corr = store.query(VerdictFilter(subject_type="correlation", limit=1))[0]
    custom = corr.metadata.custom
    assert custom["evidence_sources"]["trace_backend"] is None


def test_trace_backend_connect_error_degrades(specs_dir, tmp_path):
    """Trace backend failure does not break correlate."""
    import httpx
    from nthlayer_workers.correlate.cli import correlate_command

    store_path, trigger_id, mock_alerts, mock_breaches = _setup_trigger_and_mocks(specs_dir, tmp_path)

    mock_backend = AsyncMock()
    mock_backend.get_trace_evidence.side_effect = httpx.ConnectError("refused")

    with patch("nthlayer_workers.correlate.prometheus.fetch_alerts", side_effect=mock_alerts), \
         patch("nthlayer_workers.correlate.prometheus.fetch_metric_breaches", side_effect=mock_breaches):
        result = correlate_command(
            trigger_verdict_id=trigger_id,
            prometheus_url="http://mock:9090",
            specs_dir=str(specs_dir),
            verdict_store_path=store_path,
            trace_backend=mock_backend,
        )

    assert result == 0

    from nthlayer_common.verdicts import SQLiteVerdictStore, VerdictFilter
    store = SQLiteVerdictStore(store_path)
    corr = store.query(VerdictFilter(subject_type="correlation", limit=1))[0]
    custom = corr.metadata.custom
    assert custom["evidence_sources"]["trace_backend"] is None


def test_trace_backend_timeout_degrades(specs_dir, tmp_path):
    """Trace backend timeout does not break correlate."""
    import asyncio as aio
    from nthlayer_workers.correlate.cli import correlate_command

    store_path, trigger_id, mock_alerts, mock_breaches = _setup_trigger_and_mocks(specs_dir, tmp_path)

    mock_backend = AsyncMock()
    mock_backend.get_trace_evidence.side_effect = aio.TimeoutError()

    with patch("nthlayer_workers.correlate.prometheus.fetch_alerts", side_effect=mock_alerts), \
         patch("nthlayer_workers.correlate.prometheus.fetch_metric_breaches", side_effect=mock_breaches):
        result = correlate_command(
            trigger_verdict_id=trigger_id,
            prometheus_url="http://mock:9090",
            specs_dir=str(specs_dir),
            verdict_store_path=store_path,
            trace_backend=mock_backend,
        )

    assert result == 0

    from nthlayer_common.verdicts import SQLiteVerdictStore, VerdictFilter
    store = SQLiteVerdictStore(store_path)
    corr = store.query(VerdictFilter(subject_type="correlation", limit=1))[0]
    assert corr.metadata.custom["evidence_sources"]["trace_backend"] is None


def test_decision_record_includes_trace_context(specs_dir, tmp_path):
    """Decision record action dict and summaries include trace evidence context."""
    from unittest.mock import MagicMock
    from nthlayer_workers.correlate.cli import correlate_command

    store_path, trigger_id, mock_alerts, mock_breaches = _setup_trigger_and_mocks(specs_dir, tmp_path)
    decision_db = str(tmp_path / "decisions.db")

    mock_backend = AsyncMock()
    mock_backend.get_trace_evidence.return_value = _make_trace_evidence()
    mock_backend.aclose = AsyncMock()

    mock_write = MagicMock()

    with patch("nthlayer_workers.correlate.prometheus.fetch_alerts", side_effect=mock_alerts), \
         patch("nthlayer_workers.correlate.prometheus.fetch_metric_breaches", side_effect=mock_breaches), \
         patch("nthlayer_workers.correlate.cli.write_decision_verdict_fn", mock_write, create=True), \
         patch.dict("sys.modules", {
             "nthlayer_common.records.sqlite_store": MagicMock(),
             "nthlayer_common.records.verdict_bridge": MagicMock(write_decision_verdict=mock_write),
         }):
        result = correlate_command(
            trigger_verdict_id=trigger_id,
            prometheus_url="http://mock:9090",
            specs_dir=str(specs_dir),
            verdict_store_path=store_path,
            trace_backend=mock_backend,
            decision_store_path=decision_db,
        )

    assert result == 0
    assert mock_write.called
    call_kwargs = mock_write.call_args[1]
    assert call_kwargs["action"]["trace_backend"] == "tempo"
    assert call_kwargs["action"]["trace_services_count"] == 1
    assert "trace" in call_kwargs["summaries_technical"].lower()
    assert call_kwargs["summaries_executive"].endswith("+ traces")


def test_trace_baseline_window_passed_through(specs_dir, tmp_path):
    """trace_baseline_window param is forwarded to get_trace_evidence."""
    from datetime import timedelta
    from nthlayer_workers.correlate.cli import correlate_command

    store_path, trigger_id, mock_alerts, mock_breaches = _setup_trigger_and_mocks(specs_dir, tmp_path)

    mock_backend = AsyncMock()
    mock_backend.get_trace_evidence.return_value = _make_trace_evidence()
    mock_backend.aclose = AsyncMock()

    with patch("nthlayer_workers.correlate.prometheus.fetch_alerts", side_effect=mock_alerts), \
         patch("nthlayer_workers.correlate.prometheus.fetch_metric_breaches", side_effect=mock_breaches):
        result = correlate_command(
            trigger_verdict_id=trigger_id,
            prometheus_url="http://mock:9090",
            specs_dir=str(specs_dir),
            verdict_store_path=store_path,
            trace_backend=mock_backend,
            trace_baseline_window="2h",
        )

    assert result == 0
    # Verify baseline_window was passed as timedelta(hours=2)
    call_kwargs = mock_backend.get_trace_evidence.call_args
    assert call_kwargs[1]["baseline_window"] == timedelta(hours=2)
