"""Unit tests for the recommendations CLI subcommand (jmy.6)."""
from __future__ import annotations

import pytest


class TestArgValidation:
    """invalid_args edge cases per jmy.6 § 7."""

    def test_incident_and_from_mutually_exclusive(self, capsys):
        from nthlayer_workers.learn.cli import main

        with pytest.raises(SystemExit) as exc:
            main(["recommendations", "--incident", "inc-x", "--from", "plan.yaml"])
        assert exc.value.code != 0

    def test_pr_requires_apply_to(self, capsys, tmp_path):
        from nthlayer_workers.learn.cli import main

        with pytest.raises(SystemExit) as exc:
            main(["recommendations", "--incident", "inc-x", "--pr"])
        assert exc.value.code != 0

    def test_neither_incident_nor_from_required(self, capsys):
        from nthlayer_workers.learn.cli import main

        with pytest.raises(SystemExit) as exc:
            main(["recommendations"])
        assert exc.value.code != 0


class TestOutputFlag:
    """--output writes plan.yaml from --from input."""

    def test_from_then_output_round_trip(self, tmp_path, capsys):
        from nthlayer_workers.learn.cli import main
        from nthlayer_workers.learn.recommendations import (
            SpecRecommendation, Recommendation,
        )
        from datetime import datetime, timezone

        # Build a plan file
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
        plan_in = tmp_path / "in.yaml"
        plan_in.write_text(plan.to_yaml())

        plan_out = tmp_path / "out.yaml"
        main(["recommendations", "--from", str(plan_in), "--output", str(plan_out)])

        # Output file written + parses back to same plan
        from nthlayer_workers.learn.recommendations import parse_plan_file
        round_tripped = parse_plan_file(plan_out)
        assert round_tripped.incident == "inc-test"
        assert len(round_tripped.recommendations) == 1


class TestApplyToFlag:
    """--apply-to applies the plan to specs in the target directory."""

    def test_from_then_apply_to(self, tmp_path):
        from nthlayer_workers.learn.cli import main
        from nthlayer_workers.learn.recommendations import (
            SpecRecommendation, Recommendation,
        )
        from datetime import datetime, timezone

        # Seed manifest + plan
        specs_dir = tmp_path / "specs"
        specs_dir.mkdir()
        (specs_dir / "fraud-detect.yaml").write_text(
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
        plan_in = tmp_path / "in.yaml"
        plan_in.write_text(plan.to_yaml())

        # Run the CLI
        main([
            "recommendations",
            "--from", str(plan_in),
            "--apply-to", str(specs_dir),
        ])

        # Manifest modified
        assert "target: 98.5" in (specs_dir / "fraud-detect.yaml").read_text()
