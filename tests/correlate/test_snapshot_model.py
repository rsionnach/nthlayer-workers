"""Tests for ModelInterface — emits correlation_snapshot assessments.

Per opensrm-saun.1.2.1: the cold-path snapshot generator now produces
assessments (kind="correlation_snapshot") rather than verdicts. Tests
assert the canonical assessment shape: id, kind, service, created_at,
data.
"""
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from nthlayer_workers.correlate.snapshot.model import ModelInterface
from nthlayer_workers.correlate.types import CorrelationGroup


def _make_group(group_id="cg-001", services=None, priority=1):
    return CorrelationGroup(
        id=group_id,
        priority=priority,
        summary="Test correlation",
        services=services or ["payment-api"],
        signals=[],
        topology=None,
        change_candidates=[],
        first_seen="2026-03-01T14:00:00Z",
        last_updated="2026-03-01T14:05:00Z",
        event_count=3,
    )


def _assert_canonical_shape(assessment) -> None:
    """Every assessment must have the canonical id/kind/service/created_at/data."""
    # interpret() now returns Assessment dataclass instances, not dicts —
    # so SQLiteAssessmentStore.put() accepts them directly.
    from nthlayer_workers.observe.assessment import Assessment
    assert isinstance(assessment, Assessment)
    assert assessment.kind == "correlation_snapshot"
    assert assessment.id.startswith("csn-")
    assert assessment.service
    assert assessment.created_at is not None
    assert isinstance(assessment.data, dict)


class TestModelInterface:
    @pytest.mark.asyncio
    async def test_successful_model_call_produces_assessments(self):
        model = ModelInterface()
        model_response = json.dumps({
            "assessments": [{
                "group_id": "cg-001",
                "service": "payment-api",
                "summary": "deploy correlated with latency spike",
                "action": "flag",
                "confidence": 0.74,
                "reasoning": "temporal proximity 12 minutes",
                "tags": ["deploy", "latency"],
            }]
        })

        with patch.object(model, "_call_model", new_callable=AsyncMock, return_value=model_response):
            groups = [_make_group()]
            assessments = await model.interpret("test prompt", groups)

        assert len(assessments) == 2  # 1 child + 1 parent
        child, parent = assessments
        for a in (child, parent):
            _assert_canonical_shape(a)
        assert child.service == "payment-api"
        assert child.data["action"] == "flag"
        assert child.data["confidence"] == 0.74
        # Parent rolls children up via parent_ids (lineage anchor).
        assert parent.data["parent_ids"] == [child.id]
        assert parent.service == "__snapshot__"

    @pytest.mark.asyncio
    async def test_model_failure_returns_template_assessments(self):
        model = ModelInterface()

        with patch.object(model, "_call_model", new_callable=AsyncMock, side_effect=Exception("API down")):
            groups = [_make_group(), _make_group(group_id="cg-002")]
            assessments = await model.interpret("test prompt", groups)

        assert len(assessments) == 3  # 2 children + 1 parent
        for a in assessments:
            _assert_canonical_shape(a)
        for child in assessments[:-1]:
            assert child.data["confidence"] == 0.0
            assert "template-based" in child.data["reasoning"]
            assert "degraded" in child.data["tags"]

    @pytest.mark.asyncio
    async def test_parent_assessment_links_children_via_parent_ids(self):
        model = ModelInterface()
        model_response = json.dumps({
            "assessments": [
                {"group_id": "cg-a", "service": "a", "action": "flag", "confidence": 0.8, "reasoning": "r1"},
                {"group_id": "cg-b", "service": "b", "action": "escalate", "confidence": 0.9, "reasoning": "r2"},
            ]
        })

        with patch.object(model, "_call_model", new_callable=AsyncMock, return_value=model_response):
            groups = [_make_group(group_id="cg-a"), _make_group(group_id="cg-b")]
            assessments = await model.interpret("test prompt", groups)

        parent = assessments[-1]
        children = assessments[:-1]
        assert set(parent.data["parent_ids"]) == {c.id for c in children}

    @pytest.mark.asyncio
    async def test_escalate_bubbles_to_parent_action(self):
        model = ModelInterface()
        model_response = json.dumps({
            "assessments": [
                {"group_id": "cg-a", "action": "escalate", "confidence": 0.9, "reasoning": "critical"},
            ]
        })

        with patch.object(model, "_call_model", new_callable=AsyncMock, return_value=model_response):
            assessments = await model.interpret("test", [_make_group()])

        parent = assessments[-1]
        assert parent.data["action"] == "escalate"

    @pytest.mark.asyncio
    async def test_invalid_action_defaults_to_flag(self):
        model = ModelInterface()
        model_response = json.dumps({
            "assessments": [
                {"group_id": "cg-a", "action": "watch", "confidence": 0.5, "reasoning": "test"},
            ]
        })

        with patch.object(model, "_call_model", new_callable=AsyncMock, return_value=model_response):
            assessments = await model.interpret("test", [_make_group()])

        child = assessments[0]
        assert child.data["action"] == "flag"  # "watch" invalid → defaulted

    @pytest.mark.asyncio
    async def test_confidence_clamped(self):
        model = ModelInterface()
        model_response = json.dumps({
            "assessments": [
                {"group_id": "cg-a", "action": "flag", "confidence": 1.5, "reasoning": "test"},
            ]
        })

        with patch.object(model, "_call_model", new_callable=AsyncMock, return_value=model_response):
            assessments = await model.interpret("test", [_make_group()])

        assert assessments[0].data["confidence"] == 1.0

    @pytest.mark.asyncio
    async def test_malformed_json_falls_back_to_template(self):
        model = ModelInterface()

        with patch.object(model, "_call_model", new_callable=AsyncMock, return_value="not json"):
            assessments = await model.interpret("test", [_make_group()])

        for a in assessments:
            assert a.data["confidence"] == 0.0

    @pytest.mark.asyncio
    async def test_assessments_stored_when_store_provided(self):
        model = ModelInterface()
        model_response = json.dumps({
            "assessments": [
                {"group_id": "cg-a", "action": "flag", "confidence": 0.8, "reasoning": "test"},
            ]
        })

        mock_store = MagicMock()

        with patch.object(model, "_call_model", new_callable=AsyncMock, return_value=model_response):
            await model.interpret("test", [_make_group()], assessment_store=mock_store)

        assert mock_store.put.call_count == 2  # 1 child + 1 parent
        # Each put receives an Assessment dataclass instance with
        # kind="correlation_snapshot" — matches SQLiteAssessmentStore.put()'s
        # type contract directly (no adapter needed).
        for call in mock_store.put.call_args_list:
            stored = call.args[0]
            _assert_canonical_shape(stored)
