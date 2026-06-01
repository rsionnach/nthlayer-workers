# tests/test_respond_command.py
"""Tests for the respond CLI subcommand with verdict-triggered incidents."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
  dependencies:
    - name: feature-store
      type: database
      critical: true
"""


@pytest.fixture
def specs_dir(tmp_path):
    (tmp_path / "fraud-detect.yaml").write_text(FRAUD_SPEC)
    return tmp_path


def _make_correlation_verdict(store):
    """Create and store a mock correlation verdict."""
    from nthlayer_common.verdicts import create as verdict_create

    v = verdict_create(
        subject={
            "type": "correlation",
            "ref": "fraud-detect",
            "summary": "Correlation: 1 group across 3 services",
        },
        judgment={"action": "flag", "confidence": 0.85},
        producer={"system": "nthlayer-correlate"},
        metadata={"custom": {
            "trigger_verdict": "vrd-eval-001",
            "root_causes": [{"service": "fraud-detect", "type": "model_deploy", "confidence": 0.9}],
            "blast_radius": [
                {"service": "fraud-detect", "impact": "direct", "slo_breached": True},
                {"service": "payment-api", "impact": "downstream", "slo_breached": False},
            ],
            "groups": 1,
            "events_gathered": 5,
        }},
    )
    store.put(v)
    return v


def test_respond_command_builds_incident_from_verdict(specs_dir, tmp_path):
    """cmd_respond reads a correlation verdict and builds an incident context."""
    from nthlayer_common.verdicts import SQLiteVerdictStore

    store_path = str(tmp_path / "verdicts.db")
    store = SQLiteVerdictStore(store_path)
    corr = _make_correlation_verdict(store)

    # Mock the coordinator to avoid needing a real model
    from nthlayer_workers.respond.types import IncidentState

    async def mock_run(context):
        context.state = IncidentState.RESOLVED
        return context

    mock_coord = MagicMock()
    mock_coord.run = AsyncMock(side_effect=mock_run)
    mock_ctx_store = MagicMock()
    mock_ctx_store.close = MagicMock()

    with patch("nthlayer_workers.respond.cli._make_coordinator", return_value=(mock_coord, mock_ctx_store)):
        import argparse
        args = argparse.Namespace(
            trigger_verdict=corr.id,
            specs_dir=str(specs_dir),
            verdict_store=store_path,
            config="respond.yaml",
            notify="stdout",
            command="respond",
        )

        from nthlayer_workers.respond.cli import cmd_respond
        cmd_respond(args)

    # Verify coordinator was called with correct context
    mock_coord.run.assert_called_once()
    ctx_arg = mock_coord.run.call_args[0][0]
    assert ctx_arg.trigger_verdict_ids == [corr.id]
    assert ctx_arg.trigger_source == "nthlayer-correlate"
    assert "FRAUD-DETECT" in ctx_arg.id


def test_respond_command_missing_verdict(tmp_path):
    """Returns error when verdict doesn't exist."""
    from nthlayer_common.verdicts import SQLiteVerdictStore

    store_path = str(tmp_path / "verdicts.db")
    SQLiteVerdictStore(store_path)  # create empty store

    import argparse
    args = argparse.Namespace(
        trigger_verdict="vrd-nonexistent",
        specs_dir=str(tmp_path),
        verdict_store=store_path,
        config="respond.yaml",
        notify="stdout",
        command="respond",
    )

    from nthlayer_workers.respond.cli import cmd_respond
    result = cmd_respond(args)
    assert result == 1


def test_respond_command_severity_mapping(specs_dir, tmp_path):
    """High confidence correlation verdict maps to severity 1 (critical)."""
    from nthlayer_common.verdicts import SQLiteVerdictStore
    from nthlayer_common.verdicts import create as verdict_create

    store_path = str(tmp_path / "verdicts.db")
    store = SQLiteVerdictStore(store_path)

    # High confidence = critical severity
    v = verdict_create(
        subject={"type": "correlation", "ref": "fraud-detect", "summary": "test"},
        judgment={"action": "flag", "confidence": 0.95},
        producer={"system": "nthlayer-correlate"},
        metadata={"custom": {"blast_radius": [], "root_causes": []}},
    )
    store.put(v)

    async def mock_run(context):
        # Verify severity was mapped from confidence
        assert context.metadata["severity"] == 1  # critical (>0.8)
        from nthlayer_workers.respond.types import IncidentState
        context.state = IncidentState.RESOLVED
        return context

    mock_coord = MagicMock()
    mock_coord.run = AsyncMock(side_effect=mock_run)
    mock_ctx_store = MagicMock()
    mock_ctx_store.close = MagicMock()

    with patch("nthlayer_workers.respond.cli._make_coordinator", return_value=(mock_coord, mock_ctx_store)):
        import argparse
        args = argparse.Namespace(
            trigger_verdict=v.id,
            specs_dir=str(specs_dir),
            verdict_store=store_path,
            config="respond.yaml",
            notify="stdout",
            command="respond",
        )
        from nthlayer_workers.respond.cli import cmd_respond
        cmd_respond(args)


def test_respond_command_loads_topology_from_specs(specs_dir, tmp_path):
    """Topology is built from spec files in --specs-dir."""
    from nthlayer_common.verdicts import SQLiteVerdictStore

    store_path = str(tmp_path / "verdicts.db")
    store = SQLiteVerdictStore(store_path)
    corr = _make_correlation_verdict(store)

    captured_ctx = {}

    async def mock_run(context):
        captured_ctx.update({
            "topology": context.topology,
            "trigger_ids": context.trigger_verdict_ids,
        })
        from nthlayer_workers.respond.types import IncidentState
        context.state = IncidentState.RESOLVED
        return context

    mock_coord = MagicMock()
    mock_coord.run = AsyncMock(side_effect=mock_run)
    mock_ctx_store = MagicMock()
    mock_ctx_store.close = MagicMock()

    with patch("nthlayer_workers.respond.cli._make_coordinator", return_value=(mock_coord, mock_ctx_store)):
        import argparse
        args = argparse.Namespace(
            trigger_verdict=corr.id,
            specs_dir=str(specs_dir),
            verdict_store=store_path,
            config="respond.yaml",
            notify="stdout",
            command="respond",
        )
        from nthlayer_workers.respond.cli import cmd_respond
        cmd_respond(args)

    # Verify topology was loaded from specs
    services = [s["name"] for s in captured_ctx["topology"]["services"]]
    assert "fraud-detect" in services
