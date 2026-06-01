"""Unit tests for nthlayer_workers.learn._preview (jmy.6)."""
from __future__ import annotations


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


class TestBuildPreviewStructural:
    """build_preview for dict-valued recommendations (add_deploy_gate)."""

    def test_add_deploy_gate_preview_shape(self):
        from nthlayer_workers.learn._preview import build_preview
        from nthlayer_workers.learn._yaml import PATH_MISSING
        from nthlayer_workers.learn.recommendations import Recommendation

        rec = Recommendation(
            id="rec-deadbeef0124",
            service="fraud-detect",
            type="add_deploy_gate",
            rationale="test",
            field="spec.deployment.gates.judgment",
            current_value=None,
            proposed_value={"enabled": True, "block_on": ["reversal_rate"]},
        )
        preview = build_preview(
            manifest_path="specs/fraud-detect.yaml",
            rec=rec,
            manifest_current_value=PATH_MISSING,
        )

        assert "# File: specs/fraud-detect.yaml" in preview
        assert "# Path: spec.deployment.gates.judgment" in preview
        # New-block style with + prefix
        assert "+   judgment:" in preview
        # Sub-keys present
        assert "enabled: true" in preview
        assert "block_on:" in preview or "reversal_rate" in preview


class TestBuildPreviewDrift:
    """Drift marker when manifest's current value differs from rec's."""

    def test_drift_marker_appended_for_scalar(self):
        from nthlayer_workers.learn._preview import build_preview
        from nthlayer_workers.learn.recommendations import Recommendation

        rec = Recommendation(
            id="rec-deadbeef0125",
            service="fraud-detect",
            type="tighten_slo",
            rationale="test",
            field="spec.slos.judgment.target",
            current_value=95.0,
            proposed_value=98.5,
        )
        # Manifest has 97.0 — drift from recommendation's expected 95.0
        preview = build_preview(
            manifest_path="specs/fraud-detect.yaml",
            rec=rec,
            manifest_current_value=97.0,
        )

        assert "# WARN: manifest drifted" in preview
        assert "current=97.0" in preview
        assert "expected=95.0" in preview

    def test_no_drift_marker_when_values_match(self):
        from nthlayer_workers.learn._preview import build_preview
        from nthlayer_workers.learn.recommendations import Recommendation

        rec = Recommendation(
            id="rec-deadbeef0126",
            service="fraud-detect",
            type="tighten_slo",
            rationale="test",
            field="spec.slos.judgment.target",
            current_value=95.0,
            proposed_value=98.5,
        )
        # Manifest matches rec's expected current_value — no drift
        preview = build_preview(
            manifest_path="specs/fraud-detect.yaml",
            rec=rec,
            manifest_current_value=95.0,
        )

        assert "# WARN" not in preview
