"""Tests for model interface and verdict output."""
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


class TestModelInterface:
    @pytest.mark.asyncio
    async def test_successful_model_call_produces_verdicts(self):
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
            verdicts = await model.interpret("test prompt", groups)

        assert len(verdicts) == 2  # 1 child + 1 parent
        child = verdicts[0]
        parent = verdicts[1]
        assert child.subject.type == "correlation"
        assert child.judgment.action == "flag"
        assert child.judgment.confidence == 0.74
        assert child.producer.system == "nthlayer-correlate"
        assert parent.lineage.children == [child.id]

    @pytest.mark.asyncio
    async def test_model_failure_returns_template_verdicts(self):
        model = ModelInterface()

        with patch.object(model, "_call_model", new_callable=AsyncMock, side_effect=Exception("API down")):
            groups = [_make_group(), _make_group(group_id="cg-002")]
            verdicts = await model.interpret("test prompt", groups)

        assert len(verdicts) == 3  # 2 children + 1 parent
        for v in verdicts[:-1]:  # children
            assert v.judgment.confidence == 0.0
            assert "template-based" in v.judgment.reasoning
            assert "degraded" in v.judgment.tags

    @pytest.mark.asyncio
    async def test_verdict_lineage_parent_links_children(self):
        model = ModelInterface()
        model_response = json.dumps({
            "assessments": [
                {"group_id": "cg-a", "service": "a", "action": "flag", "confidence": 0.8, "reasoning": "r1"},
                {"group_id": "cg-b", "service": "b", "action": "escalate", "confidence": 0.9, "reasoning": "r2"},
            ]
        })

        with patch.object(model, "_call_model", new_callable=AsyncMock, return_value=model_response):
            groups = [_make_group(group_id="cg-a"), _make_group(group_id="cg-b")]
            verdicts = await model.interpret("test prompt", groups)

        parent = verdicts[-1]
        children = verdicts[:-1]
        assert len(parent.lineage.children) == 2
        assert set(parent.lineage.children) == {c.id for c in children}

    @pytest.mark.asyncio
    async def test_escalate_bubbles_to_parent(self):
        model = ModelInterface()
        model_response = json.dumps({
            "assessments": [
                {"group_id": "cg-a", "action": "escalate", "confidence": 0.9, "reasoning": "critical"},
            ]
        })

        with patch.object(model, "_call_model", new_callable=AsyncMock, return_value=model_response):
            verdicts = await model.interpret("test", [_make_group()])

        parent = verdicts[-1]
        assert parent.judgment.action == "escalate"

    @pytest.mark.asyncio
    async def test_invalid_action_defaults_to_flag(self):
        model = ModelInterface()
        model_response = json.dumps({
            "assessments": [
                {"group_id": "cg-a", "action": "watch", "confidence": 0.5, "reasoning": "test"},
            ]
        })

        with patch.object(model, "_call_model", new_callable=AsyncMock, return_value=model_response):
            verdicts = await model.interpret("test", [_make_group()])

        child = verdicts[0]
        assert child.judgment.action == "flag"  # "watch" is invalid, defaulted to "flag"

    @pytest.mark.asyncio
    async def test_confidence_clamped(self):
        model = ModelInterface()
        model_response = json.dumps({
            "assessments": [
                {"group_id": "cg-a", "action": "flag", "confidence": 1.5, "reasoning": "test"},
            ]
        })

        with patch.object(model, "_call_model", new_callable=AsyncMock, return_value=model_response):
            verdicts = await model.interpret("test", [_make_group()])

        assert verdicts[0].judgment.confidence == 1.0

    @pytest.mark.asyncio
    async def test_malformed_json_falls_back_to_template(self):
        model = ModelInterface()

        with patch.object(model, "_call_model", new_callable=AsyncMock, return_value="not json"):
            verdicts = await model.interpret("test", [_make_group()])

        assert all(v.judgment.confidence == 0.0 for v in verdicts)

    @pytest.mark.asyncio
    async def test_verdicts_stored_when_store_provided(self):
        model = ModelInterface()
        model_response = json.dumps({
            "assessments": [
                {"group_id": "cg-a", "action": "flag", "confidence": 0.8, "reasoning": "test"},
            ]
        })

        mock_store = MagicMock()

        with patch.object(model, "_call_model", new_callable=AsyncMock, return_value=model_response):
            await model.interpret("test", [_make_group()], verdict_store=mock_store)

        assert mock_store.put.call_count == 2  # 1 child + 1 parent
