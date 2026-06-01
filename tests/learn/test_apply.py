"""Unit tests for nthlayer_workers.learn._apply (jmy.6)."""
from __future__ import annotations

from datetime import UTC
from pathlib import Path

import pytest


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
        from datetime import datetime

        from nthlayer_workers.learn._apply import apply_recommendations
        from nthlayer_workers.learn.recommendations import (
            OutcomeKind,
            Recommendation,
            SpecRecommendation,
        )

        # Seed manifest
        (tmp_path / "fraud-detect.yaml").write_text(
            "metadata:\n  name: fraud-detect\n"
            "spec:\n  slos:\n    judgment:\n      target: 95.0\n"
        )
        plan = SpecRecommendation(
            incident="inc-test",
            generated_by="nthlayer-learn",
            generated_at=datetime(2026, 5, 26, tzinfo=UTC),
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
        from datetime import datetime

        from nthlayer_workers.learn._apply import apply_recommendations
        from nthlayer_workers.learn.recommendations import (
            OutcomeKind,
            Recommendation,
            SpecRecommendation,
        )

        # No manifest seeded
        plan = SpecRecommendation(
            incident="inc-test",
            generated_by="nthlayer-learn",
            generated_at=datetime(2026, 5, 26, tzinfo=UTC),
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
        from datetime import datetime

        from nthlayer_workers.learn._apply import apply_recommendations
        from nthlayer_workers.learn.recommendations import (
            Recommendation,
            SpecRecommendation,
        )

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
            generated_at=datetime(2026, 5, 26, tzinfo=UTC),
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
        from datetime import datetime

        from nthlayer_workers.learn._apply import apply_recommendations
        from nthlayer_workers.learn.recommendations import (
            Recommendation,
            SpecRecommendation,
        )

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
            generated_at=datetime(2026, 5, 26, tzinfo=UTC),
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
            ApplyResult,
            RecOutcome,
            format_summary,
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
            ApplyResult,
            RecOutcome,
            format_summary,
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
            ApplyResult,
            RecOutcome,
            format_summary,
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


class TestApplyIdempotency:
    """opensrm-1mja: ALREADY_APPLIED outcomes route to skipped (not applied).

    The apply layer must report idempotent re-runs as skips so downstream
    consumers (CLI summary, --json output, operators reading exit-code
    semantics) don't double-count no-op recs as applied work.
    """

    def test_add_dependency_rerun_routes_to_skipped(self, tmp_path):
        """Apply add_dependency once → applied. Apply same plan again →
        skipped (outcome=already_applied), manifest unchanged."""
        from datetime import datetime

        from nthlayer_workers.learn._apply import apply_recommendations
        from nthlayer_workers.learn.recommendations import (
            OutcomeKind,
            Recommendation,
            SpecRecommendation,
        )

        (tmp_path / "payments-api.yaml").write_text(
            "metadata:\n  name: payments-api\nspec:\n  slos: {}\n"
        )
        plan = SpecRecommendation(
            incident="inc-idempotency",
            generated_by="test",
            generated_at=datetime(2026, 5, 30, tzinfo=UTC),
            confidence=0.5,
            recommendations=[
                Recommendation(
                    id="rec-idem-1",
                    service="payments-api",
                    type="add_dependency",
                    rationale="test",
                    field="spec.dependencies[+]",
                    current_value=None,
                    proposed_value={"name": "svc-new", "type": "api"},
                ),
            ],
        )

        # First apply: APPLY_CLEAN → applied
        first = apply_recommendations(plan, tmp_path)
        assert len(first.applied) == 1
        assert first.applied[0].outcome == OutcomeKind.APPLY_CLEAN
        assert len(first.skipped) == 0

        # Second apply: ALREADY_APPLIED → skipped (THE FIX)
        second = apply_recommendations(plan, tmp_path)
        assert len(second.applied) == 0, (
            f"already_applied must route to skipped, not applied: "
            f"{second.applied}"
        )
        assert len(second.skipped) == 1
        assert second.skipped[0].outcome == OutcomeKind.ALREADY_APPLIED
        assert second.skipped[0].id == "rec-idem-1"
        # Idempotent re-run is success: exit_code 0, NOT 2.
        assert second.exit_code == 0

    def test_exit_code_already_applied_plus_drift_is_partial(self):
        """Boundary test (R5 correctness): ALREADY_APPLIED + DRIFT_DETECTED
        in skipped (no APPLY_CLEAN in applied) → exit 1 (partial), not 2.

        The idempotent no-op IS a clean operation for partial-success
        purposes; mixing it with a real failure should preserve the
        "partial" semantic operators rely on.
        """
        from nthlayer_workers.learn._apply import ApplyResult, RecOutcome
        from nthlayer_workers.learn.recommendations import OutcomeKind

        result = ApplyResult(
            applied=[],
            skipped=[
                RecOutcome(id="r1", service="s", field="f1",
                           outcome=OutcomeKind.ALREADY_APPLIED),
                RecOutcome(id="r2", service="s", field="f2",
                           outcome=OutcomeKind.DRIFT_DETECTED),
            ],
        )
        assert result.exit_code == 1

    def test_exit_code_only_drift_is_complete_failure(self):
        """Counter-test: no clean op (neither APPLY_CLEAN nor
        ALREADY_APPLIED) + drift → exit 2 (complete failure)."""
        from nthlayer_workers.learn._apply import ApplyResult, RecOutcome
        from nthlayer_workers.learn.recommendations import OutcomeKind

        result = ApplyResult(
            applied=[],
            skipped=[
                RecOutcome(id="r1", service="s", field="f1",
                           outcome=OutcomeKind.DRIFT_DETECTED),
            ],
        )
        assert result.exit_code == 2

    def test_exit_code_three_way_mix_is_partial(self):
        """Three-way mix (R5 edge-cases gap): APPLY_CLEAN in applied
        + ALREADY_APPLIED in skipped + DRIFT_DETECTED in skipped →
        exit 1 (partial). The boundary tests above cover the empty-
        applied cases; this pins the full mixed case the routing
        change is most likely to encounter in a re-run with new drift."""
        from nthlayer_workers.learn._apply import ApplyResult, RecOutcome
        from nthlayer_workers.learn.recommendations import OutcomeKind

        result = ApplyResult(
            applied=[
                RecOutcome(id="r1", service="s", field="f1",
                           outcome=OutcomeKind.APPLY_CLEAN),
            ],
            skipped=[
                RecOutcome(id="r2", service="s", field="f2",
                           outcome=OutcomeKind.ALREADY_APPLIED),
                RecOutcome(id="r3", service="s", field="f3",
                           outcome=OutcomeKind.DRIFT_DETECTED),
            ],
        )
        assert result.exit_code == 1

    def test_tighten_slo_rerun_routes_to_skipped(self, tmp_path):
        """Same invariant for scalar paths (tighten_slo): re-applying a
        rec whose proposed_value already matches the manifest is a no-op
        skip, not a re-application."""
        from datetime import datetime

        from nthlayer_workers.learn._apply import apply_recommendations
        from nthlayer_workers.learn.recommendations import (
            OutcomeKind,
            Recommendation,
            SpecRecommendation,
        )

        # Seed manifest where the target ALREADY equals the proposed value.
        (tmp_path / "fraud-detect.yaml").write_text(
            "metadata:\n  name: fraud-detect\n"
            "spec:\n  slos:\n    judgment:\n      target: 98.5\n"
        )
        plan = SpecRecommendation(
            incident="inc-idempotency-scalar",
            generated_by="test",
            generated_at=datetime(2026, 5, 30, tzinfo=UTC),
            confidence=0.7,
            recommendations=[
                Recommendation(
                    id="rec-idem-scalar",
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
        assert len(result.applied) == 0
        assert len(result.skipped) == 1
        assert result.skipped[0].outcome == OutcomeKind.ALREADY_APPLIED
