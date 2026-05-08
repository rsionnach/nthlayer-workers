"""Tests for the top-level `nthlayer-workers gate` command (opensrm-8jd.2).

The legacy `nthlayer-workers observe check-deploy` writes to a local
SQLite assessment store; the canonical v1.5 path is the top-level
`gate` subcommand which submits a `deploy_gate` assessment to the
core API (see decisions/legacy-cli-maintenance-mode.md).
"""

from __future__ import annotations

import argparse
from unittest.mock import AsyncMock, patch

import pytest

from nthlayer_common.api_client import APIResult


@pytest.fixture
def gate_args():
    """Minimal args namespace for _gate_async."""
    return argparse.Namespace(
        service="fraud-detect",
        tier="critical",
        commit_sha="abc1234",
        core_url="http://core:8000",
    )


def _make_manifest_response():
    return APIResult(
        ok=True, status_code=200,
        data={"name": "fraud-detect", "tier": "critical", "type": "ai-gate"},
    )


def _make_slo_assessment_dict(slo_name: str, percent_consumed: float, status: str):
    """Shape matches what get_assessments would return for a slo_status assessment."""
    return {
        "id": f"asm-{slo_name}-001",
        "kind": "slo_status",
        "service": "fraud-detect",
        "created_at": "2026-05-08T12:00:00+00:00",
        "producer": "nthlayer-observe",
        "data": {
            "slo_name": slo_name,
            "objective": 99.9,
            "window": "30d",
            "total_budget_minutes": 1440.0,
            "current_sli": 99.0,
            "burned_minutes": 100.0,
            "percent_consumed": percent_consumed,
            "status": status,
        },
    }


class TestGateCLISubmitsAssessment:
    """The gate command submits a `deploy_gate` assessment via core API."""

    @pytest.mark.asyncio
    async def test_approved_path_submits_deploy_gate_assessment(self, gate_args):
        """Healthy budget → APPROVED → exit 0 → deploy_gate assessment submitted."""
        from nthlayer_workers.cli import _gate_async

        client = AsyncMock()
        client.get_manifest = AsyncMock(return_value=_make_manifest_response())
        client.get_assessments = AsyncMock(return_value=APIResult(
            ok=True, status_code=200,
            data=[_make_slo_assessment_dict("availability", 12.0, "HEALTHY")],
        ))
        submitted = []
        client.submit_assessment = AsyncMock(
            side_effect=lambda envelope: (
                submitted.append(envelope), APIResult(ok=True, status_code=201, data={})
            )[1]
        )

        with patch("nthlayer_common.api_client.CoreAPIClient", return_value=client):
            exit_code = await _gate_async(gate_args)

        assert exit_code == 0
        assert client.submit_assessment.await_count == 1
        envelope = submitted[0]
        # Pull the inner assessment data out of the CloudEvents envelope.
        data = envelope.get("data", envelope)
        assert data.get("kind") == "deploy_gate"
        assert data.get("service") == "fraud-detect"
        inner = data.get("data", {})
        assert inner.get("decision") == "approved"
        assert inner.get("commit_sha") == "abc1234"

    @pytest.mark.asyncio
    async def test_blocked_path_still_submits_assessment(self, gate_args):
        """A BLOCKED gate result still records the assessment audit trail."""
        from nthlayer_workers.cli import _gate_async

        client = AsyncMock()
        client.get_manifest = AsyncMock(return_value=_make_manifest_response())
        # Critical tier blocks at >= 50% remaining (default policy);
        # 95% consumed = 5% remaining → BLOCKED.
        client.get_assessments = AsyncMock(return_value=APIResult(
            ok=True, status_code=200,
            data=[_make_slo_assessment_dict("availability", 95.0, "EXHAUSTED")],
        ))
        submitted = []
        client.submit_assessment = AsyncMock(
            side_effect=lambda envelope: (
                submitted.append(envelope), APIResult(ok=True, status_code=201, data={})
            )[1]
        )

        with patch("nthlayer_common.api_client.CoreAPIClient", return_value=client):
            exit_code = await _gate_async(gate_args)

        assert exit_code == 2
        assert client.submit_assessment.await_count == 1
        envelope = submitted[0]
        data = envelope.get("data", envelope)
        assert data.get("kind") == "deploy_gate"
        assert data.get("data", {}).get("decision") == "blocked"

    @pytest.mark.asyncio
    async def test_manifest_fetch_failure_short_circuits(self, gate_args):
        """If the manifest fetch fails, no assessment is submitted (fail closed before evaluation)."""
        from nthlayer_workers.cli import _gate_async

        # Args without explicit tier — _gate_async must fetch the manifest.
        gate_args.tier = None

        client = AsyncMock()
        client.get_manifest = AsyncMock(return_value=APIResult(
            ok=False, status_code=503, error="unavailable",
        ))
        client.get_assessments = AsyncMock()
        client.submit_assessment = AsyncMock()

        with patch("nthlayer_common.api_client.CoreAPIClient", return_value=client):
            exit_code = await _gate_async(gate_args)

        assert exit_code == 1
        client.submit_assessment.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_assessment_carries_parent_slo_ids(self, gate_args):
        """Submitted deploy_gate assessment includes slo_status assessment ids as parent_ids."""
        from nthlayer_workers.cli import _gate_async

        client = AsyncMock()
        client.get_manifest = AsyncMock(return_value=_make_manifest_response())
        slo_dicts = [
            _make_slo_assessment_dict("availability", 30.0, "HEALTHY"),
            _make_slo_assessment_dict("latency", 25.0, "HEALTHY"),
        ]
        client.get_assessments = AsyncMock(return_value=APIResult(
            ok=True, status_code=200, data=slo_dicts,
        ))
        submitted = []
        client.submit_assessment = AsyncMock(
            side_effect=lambda envelope: (
                submitted.append(envelope), APIResult(ok=True, status_code=201, data={})
            )[1]
        )

        with patch("nthlayer_common.api_client.CoreAPIClient", return_value=client):
            await _gate_async(gate_args)

        envelope = submitted[0]
        data = envelope.get("data", envelope)
        parent_ids = data.get("data", {}).get("parent_ids", [])
        # Both slo_status assessments are wired as parents for lineage.
        assert "asm-availability-001" in parent_ids
        assert "asm-latency-001" in parent_ids
