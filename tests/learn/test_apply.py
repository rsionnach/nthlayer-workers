"""Unit tests for nthlayer_workers.learn._apply (jmy.6)."""
from __future__ import annotations

import pytest
from pathlib import Path


class TestResolveManifestPath:
    """resolve_manifest_path: filename-convention + walk fallback."""

    def test_filename_convention(self, tmp_path):
        from nthlayer_workers.learn._apply import resolve_manifest_path

        (tmp_path / "fraud-detect.yaml").write_text(
            "metadata:\n  name: fraud-detect\nspec:\n  slos: {}\n"
        )

        result = resolve_manifest_path("fraud-detect", tmp_path)
        assert result == tmp_path / "fraud-detect.yaml"

    def test_filename_convention_yml_extension(self, tmp_path):
        from nthlayer_workers.learn._apply import resolve_manifest_path

        (tmp_path / "fraud-detect.yml").write_text(
            "metadata:\n  name: fraud-detect\nspec:\n  slos: {}\n"
        )

        result = resolve_manifest_path("fraud-detect", tmp_path)
        assert result == tmp_path / "fraud-detect.yml"

    def test_walk_fallback_finds_by_metadata_name(self, tmp_path):
        from nthlayer_workers.learn._apply import resolve_manifest_path

        # File doesn't match service name; metadata.name does
        nested = tmp_path / "payments" / "billing-srv.yaml"
        nested.parent.mkdir(parents=True)
        nested.write_text(
            "metadata:\n  name: fraud-detect\nspec:\n  slos: {}\n"
        )

        result = resolve_manifest_path("fraud-detect", tmp_path)
        assert result == nested

    def test_hidden_dirs_excluded(self, tmp_path):
        from nthlayer_workers.learn._apply import resolve_manifest_path

        # Hidden dir; should be excluded from walk
        hidden = tmp_path / ".cache" / "fraud-detect.yaml"
        hidden.parent.mkdir()
        hidden.write_text(
            "metadata:\n  name: fraud-detect\nspec:\n  slos: {}\n"
        )

        result = resolve_manifest_path("fraud-detect", tmp_path)
        assert result is None  # not found (hidden excluded)

    def test_not_found_returns_none(self, tmp_path):
        from nthlayer_workers.learn._apply import resolve_manifest_path

        result = resolve_manifest_path("nonexistent-service", tmp_path)
        assert result is None


class TestApplyHappyPath:
    """apply_recommendations orchestration: happy path."""

    def test_single_rec_applied(self, tmp_path):
        from nthlayer_workers.learn._apply import apply_recommendations
        from nthlayer_workers.learn.recommendations import (
            Recommendation, SpecRecommendation, OutcomeKind,
        )
        from datetime import datetime, timezone

        # Seed manifest
        (tmp_path / "fraud-detect.yaml").write_text(
            "metadata:\n  name: fraud-detect\n"
            "spec:\n  slos:\n    judgment:\n      target: 95.0\n"
        )
        plan = SpecRecommendation(
            incident="inc-test",
            generated_by="nthlayer-learn",
            generated_at=datetime(2026, 5, 26, tzinfo=timezone.utc),
            confidence=0.7,
            recommendations=[
                Recommendation(
                    id="rec-deadbeef0123",
                    service="fraud-detect",
                    type="tighten_slo",
                    rationale="test",
                    field="spec.slos.judgment.target",
                    current_value=95.0,
                    proposed_value=98.5,
                ),
            ],
        )

        result = apply_recommendations(plan, tmp_path)

        assert len(result.applied) == 1
        assert result.applied[0].id == "rec-deadbeef0123"
        assert result.applied[0].outcome == OutcomeKind.APPLY_CLEAN
        assert len(result.skipped) == 0
        # Manifest file modified on disk
        assert "target: 98.5" in (tmp_path / "fraud-detect.yaml").read_text()
        assert "target: 95.0" not in (tmp_path / "fraud-detect.yaml").read_text()

    def test_skipped_when_manifest_missing(self, tmp_path):
        from nthlayer_workers.learn._apply import apply_recommendations
        from nthlayer_workers.learn.recommendations import (
            Recommendation, SpecRecommendation, OutcomeKind,
        )
        from datetime import datetime, timezone

        # No manifest seeded
        plan = SpecRecommendation(
            incident="inc-test",
            generated_by="nthlayer-learn",
            generated_at=datetime(2026, 5, 26, tzinfo=timezone.utc),
            confidence=0.7,
            recommendations=[
                Recommendation(
                    id="rec-deadbeef0124",
                    service="unknown-service",
                    type="tighten_slo",
                    rationale="test",
                    field="spec.slos.judgment.target",
                    current_value=95.0,
                    proposed_value=98.5,
                ),
            ],
        )

        result = apply_recommendations(plan, tmp_path)
        assert len(result.applied) == 0
        assert len(result.skipped) == 1
        assert result.skipped[0].outcome == OutcomeKind.MANIFEST_NOT_FOUND
