"""Unit tests for nthlayer_workers.learn._preview (jmy.6)."""
from __future__ import annotations

import pytest


class TestBuildPreviewScalar:
    """build_preview for scalar-valued recommendations (tighten_slo)."""

    def test_scalar_change_preview_shape(self):
        from nthlayer_workers.learn._preview import build_preview
        from nthlayer_workers.learn.recommendations import Recommendation

        rec = Recommendation(
            id="rec-deadbeef0123",
            service="fraud-detect",
            type="tighten_slo",
            rationale="test",
            field="spec.slos.judgment.target",
            current_value=95.0,
            proposed_value=98.5,
        )
        # Manifest's current value (read by _apply before calling build_preview)
        preview = build_preview(
            manifest_path="specs/fraud-detect.yaml",
            rec=rec,
            manifest_current_value=95.0,
        )

        # Heading lines pinned per jmy.6 design § 6.1
        assert "# File: specs/fraud-detect.yaml" in preview
        assert "# Path: spec.slos.judgment.target" in preview
        # Unified-diff style
        assert "-   target: 95.0" in preview
        assert "+   target: 98.5" in preview

    def test_scalar_already_applied_returns_empty(self):
        """When manifest already has proposed value, preview is empty."""
        from nthlayer_workers.learn._preview import build_preview
        from nthlayer_workers.learn.recommendations import Recommendation

        rec = Recommendation(
            id="rec-deadbeef0123",
            service="fraud-detect",
            type="tighten_slo",
            rationale="test",
            field="spec.slos.judgment.target",
            current_value=95.0,
            proposed_value=98.5,
        )
        preview = build_preview(
            manifest_path="specs/fraud-detect.yaml",
            rec=rec,
            manifest_current_value=98.5,  # already matches proposed
        )
        # Empty preview (no diff to show); caller suppresses the field
        assert preview == ""
