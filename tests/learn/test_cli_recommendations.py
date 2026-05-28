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


class TestPrPath:
    """--pr drives pre-flight + branch + commit + push + gh pr create."""

    def test_pr_path_happy(self, tmp_path, monkeypatch, capsys):
        """End-to-end --pr with all git/gh subprocess calls stubbed."""
        from nthlayer_workers.learn.cli import main
        from nthlayer_workers.learn.recommendations import (
            SpecRecommendation, Recommendation,
        )
        from datetime import datetime, timezone
        import subprocess

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

        # Stub every subprocess.run invocation (gh + git)
        captured: list = []
        def fake_run(args, **kwargs):
            captured.append(list(args))
            # gh --version: success
            if args[:2] == ["gh", "--version"]:
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="gh 2.x", stderr="")
            # gh auth status: success
            if args[:3] == ["gh", "auth", "status"]:
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="logged in", stderr="")
            # git rev-parse --git-dir: success (simulate repo)
            if "rev-parse" in args:
                return subprocess.CompletedProcess(args=args, returncode=0, stdout=".git", stderr="")
            # git remote get-url origin: success
            if "remote" in args and "get-url" in args:
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="git@github.com:org/repo.git", stderr="")
            # branch checks: not exists
            if "show-ref" in args:
                return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="")
            if "ls-remote" in args:
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
            # All git commands (checkout, add, commit, push) succeed
            if args[0] == "git":
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
            # gh pr create: success
            if args[:3] == ["gh", "pr", "create"]:
                return subprocess.CompletedProcess(
                    args=args, returncode=0,
                    stdout="https://github.com/org/repo/pull/99\n",
                    stderr="",
                )
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)

        # Run the CLI with --pr
        # Happy path: all recs applied (exit_code=0), _cmd_recommendations returns normally
        main([
            "recommendations",
            "--from", str(plan_in),
            "--apply-to", str(specs_dir),
            "--pr",
        ])

        # Verify gh pr create was called
        gh_pr_create_called = any(
            list(c[:3]) == ["gh", "pr", "create"] for c in captured
        )
        assert gh_pr_create_called

        # stdout contains PR URL
        captured_out = capsys.readouterr()
        assert "https://github.com/org/repo/pull/99" in captured_out.out


class TestExitCodes:
    """Pass 1 R5 fix: SystemExit must use int exit codes per spec § 7."""

    def test_preflight_failure_exits_2(self, tmp_path, monkeypatch, capsys):
        """gh not installed → exits 2, not 1."""
        from nthlayer_workers.learn.cli import main
        from nthlayer_workers.learn.recommendations import (
            SpecRecommendation, Recommendation,
        )
        from datetime import datetime, timezone
        import subprocess

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

        # Make gh look not-installed so pre-flight raises PreflightError
        def fake_run(args, **kwargs):
            if args[:1] == ["gh"]:
                raise FileNotFoundError("no gh")
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)

        with pytest.raises(SystemExit) as exc_info:
            main([
                "recommendations",
                "--from", str(plan_in),
                "--apply-to", str(specs_dir),
                "--pr",
            ])

        # Critical: exit code must be the integer 2, not the truthy string "...Exit code: 2"
        assert exc_info.value.code == 2

    def test_pr_path_propagates_partial_skip_exit_code(self, tmp_path, monkeypatch):
        """When --pr succeeds but --apply-to had a partial-skip, exit code is 1 not 0."""
        from nthlayer_workers.learn.cli import main
        from nthlayer_workers.learn.recommendations import (
            SpecRecommendation, Recommendation,
        )
        from datetime import datetime, timezone
        import subprocess

        specs_dir = tmp_path / "specs"
        specs_dir.mkdir()

        # One manifest exists (fraud-detect) → will get APPLY_CLEAN
        # One manifest missing (missing-svc) → will get MANIFEST_NOT_FOUND → skipped
        (specs_dir / "fraud-detect.yaml").write_text(
            "metadata:\n  name: fraud-detect\n"
            "spec:\n  slos:\n    judgment:\n      target: 95.0\n"
        )

        plan = SpecRecommendation(
            incident="inc-partial",
            generated_by="nthlayer-learn",
            generated_at=datetime(2026, 5, 26, tzinfo=timezone.utc),
            confidence=0.7,
            recommendations=[
                Recommendation(
                    id="rec-deadbeef0001",
                    service="fraud-detect",
                    type="tighten_slo",
                    rationale="test",
                    field="spec.slos.judgment.target",
                    current_value=95.0,
                    proposed_value=98.5,
                ),
                Recommendation(
                    id="rec-deadbeef0002",
                    service="missing-svc",
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

        # Stub all subprocess calls (gh + git) to succeed
        def fake_run(args, **kwargs):
            if args[:2] == ["gh", "--version"]:
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="gh 2.x", stderr="")
            if args[:3] == ["gh", "auth", "status"]:
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="logged in", stderr="")
            if "rev-parse" in args:
                return subprocess.CompletedProcess(args=args, returncode=0, stdout=".git", stderr="")
            if "remote" in args and "get-url" in args:
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="git@github.com:org/repo.git", stderr="")
            if "show-ref" in args:
                return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="")
            if "ls-remote" in args:
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
            if args[:3] == ["gh", "pr", "create"]:
                return subprocess.CompletedProcess(
                    args=args, returncode=0,
                    stdout="https://github.com/org/repo/pull/42\n",
                    stderr="",
                )
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)

        with pytest.raises(SystemExit) as exc_info:
            main([
                "recommendations",
                "--from", str(plan_in),
                "--apply-to", str(specs_dir),
                "--pr",
            ])

        # Partial-skip (1 applied + 1 skipped) → exit code 1, not 0
        assert exc_info.value.code == 1
