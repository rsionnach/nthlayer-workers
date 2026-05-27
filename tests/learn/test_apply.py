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


class TestApplyAtomicity:
    """Atomic write phase: alphabetical order, failure isolation."""

    def test_alphabetical_write_order(self, tmp_path):
        """Files are written in alphabetical order, not encounter order."""
        from nthlayer_workers.learn._apply import apply_recommendations
        from nthlayer_workers.learn.recommendations import (
            Recommendation, SpecRecommendation,
        )
        from datetime import datetime, timezone

        # Seed two manifests
        (tmp_path / "z-service.yaml").write_text(
            "metadata:\n  name: z-service\n"
            "spec:\n  slos:\n    judgment:\n      target: 95.0\n"
        )
        (tmp_path / "a-service.yaml").write_text(
            "metadata:\n  name: a-service\n"
            "spec:\n  slos:\n    judgment:\n      target: 95.0\n"
        )

        # Recommend changes; z-service first in plan, a-service second
        plan = SpecRecommendation(
            incident="inc-test",
            generated_by="nthlayer-learn",
            generated_at=datetime(2026, 5, 26, tzinfo=timezone.utc),
            confidence=0.7,
            recommendations=[
                Recommendation(
                    id="rec-z000",
                    service="z-service",
                    type="tighten_slo",
                    rationale="test",
                    field="spec.slos.judgment.target",
                    current_value=95.0,
                    proposed_value=98.5,
                ),
                Recommendation(
                    id="rec-a000",
                    service="a-service",
                    type="tighten_slo",
                    rationale="test",
                    field="spec.slos.judgment.target",
                    current_value=95.0,
                    proposed_value=98.5,
                ),
            ],
        )

        result = apply_recommendations(plan, tmp_path)

        # Both applied
        assert len(result.applied) == 2
        # modified_files in alphabetical order
        names = [p.name for p in result.modified_files]
        assert names == ["a-service.yaml", "z-service.yaml"]

    def test_filename_based_failure_injection_isolates_failure(
        self, tmp_path, monkeypatch,
    ):
        """Filename-based failure injection per jmy.6 design § 8."""
        from nthlayer_workers.learn._apply import apply_recommendations
        from nthlayer_workers.learn.recommendations import (
            Recommendation, SpecRecommendation,
        )
        from datetime import datetime, timezone

        # Seed two manifests
        (tmp_path / "a-service.yaml").write_text(
            "metadata:\n  name: a-service\n"
            "spec:\n  slos:\n    judgment:\n      target: 95.0\n"
        )
        (tmp_path / "b-service.yaml").write_text(
            "metadata:\n  name: b-service\n"
            "spec:\n  slos:\n    judgment:\n      target: 95.0\n"
        )

        # Capture original write_text before monkeypatching
        original_write_text = Path.write_text

        def selective_fail(self, content, **kwargs):
            if self.name == "b-service.yaml":
                raise OSError("simulated write failure on b-service.yaml")
            return original_write_text(self, content, **kwargs)

        monkeypatch.setattr(Path, "write_text", selective_fail)

        plan = SpecRecommendation(
            incident="inc-test",
            generated_by="nthlayer-learn",
            generated_at=datetime(2026, 5, 26, tzinfo=timezone.utc),
            confidence=0.7,
            recommendations=[
                Recommendation(
                    id="rec-a000",
                    service="a-service",
                    type="tighten_slo",
                    rationale="test",
                    field="spec.slos.judgment.target",
                    current_value=95.0,
                    proposed_value=98.5,
                ),
                Recommendation(
                    id="rec-b000",
                    service="b-service",
                    type="tighten_slo",
                    rationale="test",
                    field="spec.slos.judgment.target",
                    current_value=95.0,
                    proposed_value=98.5,
                ),
            ],
        )

        with pytest.raises(OSError, match="b-service.yaml"):
            apply_recommendations(plan, tmp_path)

        # a-service.yaml was modified before the write failure
        assert "target: 98.5" in (tmp_path / "a-service.yaml").read_text()
        # b-service.yaml original content retained
        assert "target: 95.0" in (tmp_path / "b-service.yaml").read_text()


class TestSummaryBuilder:
    """End-of-run summary string format per jmy.6 § 6.2."""

    def test_summary_applied_section(self):
        from nthlayer_workers.learn._apply import (
            ApplyResult, RecOutcome, format_summary,
        )
        from nthlayer_workers.learn.recommendations import OutcomeKind

        result = ApplyResult(
            applied=[
                RecOutcome(
                    id="rec-a3f8b2e1c9d4",
                    service="fraud-detect",
                    field="spec.slos.judgment.target",
                    outcome=OutcomeKind.APPLY_CLEAN,
                ),
            ],
        )
        summary = format_summary(result)

        assert "Applied: 1" in summary
        assert "rec-a3f8b2e1c9d4" in summary
        assert "fraud-detect" in summary
        assert "spec.slos.judgment.target" in summary

    def test_summary_skipped_section_with_drift_detail(self):
        from nthlayer_workers.learn._apply import (
            ApplyResult, RecOutcome, format_summary,
        )
        from nthlayer_workers.learn.recommendations import OutcomeKind

        result = ApplyResult(
            applied=[],
            skipped=[
                RecOutcome(
                    id="rec-d5e8f2b6c9a1",
                    service="notification",
                    field="spec.slos.availability.target",
                    outcome=OutcomeKind.DRIFT_DETECTED,
                    detail="manifest current: 98.0\nrecommendation expected: 95.0\nproposed value: 99.0",
                ),
            ],
        )
        summary = format_summary(result)

        assert "Skipped: 1" in summary
        assert "drift_detected" in summary
        assert "Re-run with --force" in summary or "--force rec-d5e8f2b6c9a1" in summary

    def test_summary_exit_code_line(self):
        from nthlayer_workers.learn._apply import (
            ApplyResult, RecOutcome, format_summary,
        )
        from nthlayer_workers.learn.recommendations import OutcomeKind

        result = ApplyResult(
            applied=[
                RecOutcome(id="rec-1", service="s", field="f", outcome=OutcomeKind.APPLY_CLEAN),
            ],
            skipped=[
                RecOutcome(id="rec-2", service="s", field="f", outcome=OutcomeKind.DRIFT_DETECTED),
            ],
        )
        summary = format_summary(result)
        # exit_code == 1 (partial)
        assert "Exit code: 1" in summary

    def test_empty_plan_summary(self):
        from nthlayer_workers.learn._apply import ApplyResult, format_summary

        summary = format_summary(ApplyResult())
        assert "Applied: 0" in summary
        assert "Exit code: 0" in summary
