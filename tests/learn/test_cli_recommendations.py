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


class TestIncidentIdValidation:
    """jmy.6 R5-P3: pre-flight reject incident IDs with unsafe characters."""

    def _build_plan_with_incident(self, tmp_path, incident_id: str):
        from nthlayer_workers.learn.recommendations import (
            SpecRecommendation, Recommendation,
        )
        from datetime import datetime, timezone

        plan = SpecRecommendation(
            incident=incident_id,
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
        return plan_in

    def test_path_traversal_rejected(self, tmp_path, monkeypatch, capsys):
        from nthlayer_workers.learn.cli import main
        # Seed a manifest so apply_recommendations produces modified_files
        specs_dir = tmp_path / "specs"
        specs_dir.mkdir()
        (specs_dir / "fraud-detect.yaml").write_text(
            "metadata:\n  name: fraud-detect\n"
            "spec:\n  slos:\n    judgment:\n      target: 95.0\n"
        )
        plan_in = self._build_plan_with_incident(tmp_path, "../../etc/passwd")

        with pytest.raises(SystemExit) as exc_info:
            main([
                "recommendations",
                "--from", str(plan_in),
                "--apply-to", str(specs_dir),
                "--pr",
            ])
        assert exc_info.value.code == 2

    def test_shell_metachar_rejected(self, tmp_path, monkeypatch, capsys):
        from nthlayer_workers.learn.cli import main
        specs_dir = tmp_path / "specs"
        specs_dir.mkdir()
        (specs_dir / "fraud-detect.yaml").write_text(
            "metadata:\n  name: fraud-detect\n"
            "spec:\n  slos:\n    judgment:\n      target: 95.0\n"
        )
        plan_in = self._build_plan_with_incident(tmp_path, "inc;rm-rf")

        with pytest.raises(SystemExit) as exc_info:
            main([
                "recommendations",
                "--from", str(plan_in),
                "--apply-to", str(specs_dir),
                "--pr",
            ])
        assert exc_info.value.code == 2

    def test_canonical_incident_id_accepted(self, tmp_path, monkeypatch, capsys):
        """Sanity: well-formed IDs flow through to the next stage."""
        from nthlayer_workers.learn.cli import main
        import subprocess

        specs_dir = tmp_path / "specs"
        specs_dir.mkdir()
        (specs_dir / "fraud-detect.yaml").write_text(
            "metadata:\n  name: fraud-detect\n"
            "spec:\n  slos:\n    judgment:\n      target: 95.0\n"
        )
        plan_in = self._build_plan_with_incident(tmp_path, "inc-2026-05-28-001")

        # Stub every subprocess to succeed
        def fake_run(args, **kwargs):
            if args[:2] == ["gh", "--version"]:
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="2.0", stderr="")
            if args[:3] == ["gh", "auth", "status"]:
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
            if args[:3] == ["gh", "pr", "create"]:
                return subprocess.CompletedProcess(
                    args=args, returncode=0,
                    stdout="https://github.com/x/y/pull/1\n", stderr="",
                )
            # All git ops succeed; branch checks return "not exists"
            if "show-ref" in args:
                return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="")
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)

        # Should NOT raise SystemExit(2) for the validation reason; may raise SystemExit(0) or similar
        try:
            main([
                "recommendations",
                "--from", str(plan_in),
                "--apply-to", str(specs_dir),
                "--pr",
            ])
        except SystemExit as exc:
            # Acceptable: 0 (full success). Reject 2 (validation should not fire here).
            assert exc.code != 2


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


class TestJsonOutput:
    """opensrm-jmy.25: --json structured output for the recommendations CLI.

    Contract:
      - --json requires --apply-to (pre-flight error mirrors --pr).
      - End-of-run emits a single JSON document on stdout.
      - format_summary continues to go to stderr.
      - The human-readable "PR created: ..." stdout line is suppressed
        in --json mode (URL surfaces inside the JSON instead).
      - PR-failure shape adds a "pr_error" key; exit_code=1.
    """

    @staticmethod
    def _build_single_rec_plan(tmp_path, *, incident: str = "inc-jmy25"):
        """Helper: write a one-recommendation plan.yaml and return its path."""
        from nthlayer_workers.learn.recommendations import (
            SpecRecommendation, Recommendation,
        )
        from datetime import datetime, timezone

        plan = SpecRecommendation(
            incident=incident,
            generated_by="nthlayer-learn",
            generated_at=datetime(2026, 5, 28, tzinfo=timezone.utc),
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
        return plan_in

    @staticmethod
    def _seed_specs_dir(tmp_path):
        specs_dir = tmp_path / "specs"
        specs_dir.mkdir()
        (specs_dir / "fraud-detect.yaml").write_text(
            "metadata:\n  name: fraud-detect\n"
            "spec:\n  slos:\n    judgment:\n      target: 95.0\n"
        )
        return specs_dir

    @staticmethod
    def _build_pr_fake_run(*, pr_url: str = "https://github.com/x/y/pull/42",
                           pr_stderr: str | None = None):
        """Build a subprocess.run stub that walks the full --pr workflow.

        If pr_stderr is None → gh pr create succeeds with pr_url on stdout.
        If pr_stderr is set  → gh pr create exits non-zero, stderr=pr_stderr.
        """
        import subprocess

        def fake_run(args, **kwargs):
            if args[:2] == ["gh", "--version"]:
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="gh 2.x", stderr="")
            if args[:3] == ["gh", "auth", "status"]:
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="logged in", stderr="")
            if "rev-parse" in args:
                return subprocess.CompletedProcess(args=args, returncode=0, stdout=".git", stderr="")
            if "remote" in args and "get-url" in args:
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="git@github.com:x/y.git", stderr="")
            if "show-ref" in args:
                return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="")
            if "ls-remote" in args:
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
            if args[:3] == ["gh", "pr", "create"]:
                if pr_stderr is not None:
                    return subprocess.CompletedProcess(
                        args=args, returncode=1, stdout="", stderr=pr_stderr,
                    )
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout=f"{pr_url}\n", stderr="",
                )
            # All other git subcommands succeed
            if args[0] == "git":
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        return fake_run

    def test_json_requires_apply_to(self, tmp_path, capsys):
        """--json without --apply-to raises SystemExit mirroring --pr's guard."""
        from nthlayer_workers.learn.cli import main

        plan_in = self._build_single_rec_plan(tmp_path)

        with pytest.raises(SystemExit) as exc_info:
            main([
                "recommendations",
                "--from", str(plan_in),
                "--json",
            ])

        # Message-shape contract from the implementation bead.
        # SystemExit may carry the message as .code (str) for argparse-style exits.
        msg = str(exc_info.value.code) if exc_info.value.code is not None else ""
        assert "--json requires --apply-to" in msg

    def test_json_apply_clean_emits_valid_json_to_stdout(self, tmp_path, capsys):
        """Happy --apply-to --json: stdout parses to the documented shape."""
        import json
        from nthlayer_workers.learn.cli import main

        specs_dir = self._seed_specs_dir(tmp_path)
        plan_in = self._build_single_rec_plan(tmp_path)

        main([
            "recommendations",
            "--from", str(plan_in),
            "--apply-to", str(specs_dir),
            "--json",
        ])

        out = capsys.readouterr().out
        doc = json.loads(out)
        assert isinstance(doc, dict)
        assert len(doc["applied"]) == 1
        entry = doc["applied"][0]
        assert {"id", "service", "field"} <= set(entry.keys())
        assert entry["id"] == "rec-deadbeef0123"
        assert entry["service"] == "fraud-detect"
        assert entry["field"] == "spec.slos.judgment.target"
        assert doc["skipped"] == []
        assert doc["pr_url"] is None
        assert doc["pr_number"] is None
        assert doc["exit_code"] == 0
        assert "pr_error" not in doc

    def test_json_with_skipped_recs_round_trips_outcome_and_detail(
        self, tmp_path, capsys,
    ):
        """Skipped recommendations surface as JSON entries with outcome+detail.

        We trigger MANIFEST_NOT_FOUND naturally by referencing a service whose
        manifest isn't in specs_dir — apply_recommendations populates the
        detail string with the canonical "no manifest for service ..." text,
        which the --json emitter must round-trip verbatim.
        """
        import json
        from nthlayer_workers.learn.cli import main
        from nthlayer_workers.learn.recommendations import (
            SpecRecommendation, Recommendation,
        )
        from datetime import datetime, timezone

        # specs_dir is empty — every recommendation will be MANIFEST_NOT_FOUND.
        specs_dir = tmp_path / "specs"
        specs_dir.mkdir()

        plan = SpecRecommendation(
            incident="inc-skip-detail",
            generated_by="nthlayer-learn",
            generated_at=datetime(2026, 5, 28, tzinfo=timezone.utc),
            confidence=0.7,
            recommendations=[
                Recommendation(
                    id="rec-skipped-0001",
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

        with pytest.raises(SystemExit):
            # Zero APPLY_CLEAN + skipped → exit 2 per ApplyResult.exit_code
            main([
                "recommendations",
                "--from", str(plan_in),
                "--apply-to", str(specs_dir),
                "--json",
            ])

        out = capsys.readouterr().out
        doc = json.loads(out)
        assert len(doc["skipped"]) == 1
        skipped = doc["skipped"][0]
        assert {"id", "service", "outcome", "detail"} <= set(skipped.keys())
        assert skipped["id"] == "rec-skipped-0001"
        assert skipped["service"] == "missing-svc"
        # OutcomeKind is a StrEnum: value is "manifest_not_found"
        assert skipped["outcome"] == "manifest_not_found"
        assert "missing-svc" in skipped["detail"]

    def test_json_with_pr_happy_path_includes_pr_url_and_number(
        self, tmp_path, monkeypatch, capsys,
    ):
        """--apply-to --pr --json: pr_url/pr_number populated from gh output."""
        import json
        import subprocess
        from nthlayer_workers.learn.cli import main

        specs_dir = self._seed_specs_dir(tmp_path)
        plan_in = self._build_single_rec_plan(tmp_path)

        monkeypatch.setattr(
            subprocess, "run",
            self._build_pr_fake_run(pr_url="https://github.com/x/y/pull/42"),
        )

        main([
            "recommendations",
            "--from", str(plan_in),
            "--apply-to", str(specs_dir),
            "--pr",
            "--json",
        ])

        out = capsys.readouterr().out
        doc = json.loads(out)
        assert doc["pr_url"] == "https://github.com/x/y/pull/42"
        assert doc["pr_number"] == 42
        assert doc["exit_code"] == 0
        assert "pr_error" not in doc

    def test_json_with_pr_failure_includes_pr_error_and_exit_code_one(
        self, tmp_path, monkeypatch, capsys,
    ):
        """gh pr create failure: pr_error populated, nulls for url/number, exit 1."""
        import json
        import subprocess
        from nthlayer_workers.learn.cli import main

        specs_dir = self._seed_specs_dir(tmp_path)
        plan_in = self._build_single_rec_plan(tmp_path)

        monkeypatch.setattr(
            subprocess, "run",
            self._build_pr_fake_run(pr_stderr="gh: command failed: rate limited"),
        )

        with pytest.raises(SystemExit) as exc_info:
            main([
                "recommendations",
                "--from", str(plan_in),
                "--apply-to", str(specs_dir),
                "--pr",
                "--json",
            ])

        # Process must exit 1 on PR failure even in --json mode.
        assert exc_info.value.code == 1

        out = capsys.readouterr().out
        doc = json.loads(out)
        assert doc["pr_url"] is None
        assert doc["pr_number"] is None
        assert isinstance(doc["pr_error"], str)
        # Contract: error string mentions the failing command.
        assert "gh pr create" in doc["pr_error"]
        assert doc["exit_code"] == 1

    def test_json_pr_error_escapes_special_characters(
        self, tmp_path, monkeypatch, capsys,
    ):
        """jmy.25 P3: pr_error containing JSON-special chars survives round-trip.

        gh stderr can include quotes, backslashes, and newlines. The JSON
        contract requires that consumers can `json.loads` stdout cleanly
        regardless of stderr content.
        """
        import json
        import subprocess
        from nthlayer_workers.learn.cli import main

        specs_dir = self._seed_specs_dir(tmp_path)
        plan_in = self._build_single_rec_plan(tmp_path)
        tricky_stderr = 'gh: failed: "quoted"\nwith backslash: C:\\path\nand newlines'

        monkeypatch.setattr(
            subprocess, "run",
            self._build_pr_fake_run(pr_stderr=tricky_stderr),
        )

        with pytest.raises(SystemExit):
            main([
                "recommendations",
                "--from", str(plan_in),
                "--apply-to", str(specs_dir),
                "--pr",
                "--json",
            ])

        out = capsys.readouterr().out
        doc = json.loads(out)
        # All special characters round-trip through json.dumps/json.loads.
        assert '"quoted"' in doc["pr_error"]
        assert "C:\\path" in doc["pr_error"]
        assert "\n" in doc["pr_error"]

    def test_json_with_all_already_applied_and_pr_no_pr_attempted(
        self, tmp_path, monkeypatch, capsys,
    ):
        """jmy.25 P3: ALREADY_APPLIED only + --pr + --json → no PR, clean JSON.

        When apply produces no modified_files (everything already at proposed
        value), _run_pr_path returns None per design. JSON should reflect
        pr_url/number=null with no pr_error and exit_code=0.
        """
        import json
        import subprocess
        from nthlayer_workers.learn.cli import main

        # Seed manifest already at the proposed_value (98.5) so the rec
        # classifies ALREADY_APPLIED, not APPLY_CLEAN.
        specs_dir = tmp_path / "specs"
        specs_dir.mkdir()
        (specs_dir / "fraud-detect.yaml").write_text(
            "metadata:\n  name: fraud-detect\n"
            "spec:\n  slos:\n    judgment:\n      target: 98.5\n"
        )
        plan_in = self._build_single_rec_plan(tmp_path)

        # subprocess.run should never be called for gh pr create — guard it.
        monkeypatch.setattr(
            subprocess, "run",
            self._build_pr_fake_run(),
        )

        main([
            "recommendations",
            "--from", str(plan_in),
            "--apply-to", str(specs_dir),
            "--pr",
            "--json",
        ])

        out = capsys.readouterr().out
        doc = json.loads(out)
        assert doc["pr_url"] is None
        assert doc["pr_number"] is None
        assert "pr_error" not in doc
        assert doc["exit_code"] == 0

    def test_json_format_summary_still_on_stderr(self, tmp_path, capsys):
        """format_summary continues to go to stderr in --json mode."""
        from nthlayer_workers.learn.cli import main

        specs_dir = self._seed_specs_dir(tmp_path)
        plan_in = self._build_single_rec_plan(tmp_path)

        main([
            "recommendations",
            "--from", str(plan_in),
            "--apply-to", str(specs_dir),
            "--json",
        ])

        captured = capsys.readouterr()
        # Distinctive token from format_summary() — it always emits "Exit code: N".
        assert "Exit code:" in captured.err

    def test_json_suppresses_human_readable_pr_url_stdout_line(
        self, tmp_path, monkeypatch, capsys,
    ):
        """--json suppresses the `print(f"PR created: {pr.url}")` line.

        Strongest assertion: captured stdout parses cleanly as a SINGLE JSON
        document — i.e. nothing concatenated after. We assert both: one line
        only, and json.loads succeeds without raising.
        """
        import json
        import subprocess
        from nthlayer_workers.learn.cli import main

        specs_dir = self._seed_specs_dir(tmp_path)
        plan_in = self._build_single_rec_plan(tmp_path)

        monkeypatch.setattr(
            subprocess, "run",
            self._build_pr_fake_run(pr_url="https://github.com/x/y/pull/42"),
        )

        main([
            "recommendations",
            "--from", str(plan_in),
            "--apply-to", str(specs_dir),
            "--pr",
            "--json",
        ])

        out = capsys.readouterr().out
        # Must parse — defends against trailing "PR created: ..." text.
        json.loads(out)
        # And must be a single logical line of output.
        assert len(out.strip().splitlines()) == 1
        # Belt-and-braces: the legacy human-readable token must NOT appear.
        assert "PR created:" not in out

    def test_json_exit_codes_match_non_json_mode(self, tmp_path, capsys):
        """--json must not change exit-code routing for the same apply outcome.

        Set up a partial-skip scenario (one applied, one MANIFEST_NOT_FOUND)
        and assert exit code is 1 in BOTH --apply-to alone and --apply-to --json.
        """
        from nthlayer_workers.learn.cli import main
        from nthlayer_workers.learn.recommendations import (
            SpecRecommendation, Recommendation,
        )
        from datetime import datetime, timezone

        def _seed_partial(root):
            specs_dir = root / "specs"
            specs_dir.mkdir()
            (specs_dir / "fraud-detect.yaml").write_text(
                "metadata:\n  name: fraud-detect\n"
                "spec:\n  slos:\n    judgment:\n      target: 95.0\n"
            )
            plan = SpecRecommendation(
                incident="inc-partial",
                generated_by="nthlayer-learn",
                generated_at=datetime(2026, 5, 28, tzinfo=timezone.utc),
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
            plan_in = root / "in.yaml"
            plan_in.write_text(plan.to_yaml())
            return specs_dir, plan_in

        # Run 1: --apply-to alone (no --json) → expect exit 1
        root_a = tmp_path / "a"
        root_a.mkdir()
        specs_a, plan_a = _seed_partial(root_a)
        with pytest.raises(SystemExit) as exc_no_json:
            main([
                "recommendations",
                "--from", str(plan_a),
                "--apply-to", str(specs_a),
            ])
        non_json_code = exc_no_json.value.code

        # Run 2: --apply-to --json → must match
        root_b = tmp_path / "b"
        root_b.mkdir()
        specs_b, plan_b = _seed_partial(root_b)
        with pytest.raises(SystemExit) as exc_json:
            main([
                "recommendations",
                "--from", str(plan_b),
                "--apply-to", str(specs_b),
                "--json",
            ])
        json_code = exc_json.value.code

        assert non_json_code == 1
        assert json_code == 1
        assert non_json_code == json_code


class TestIncludeExcludeFlags:
    """opensrm-jmy.24: --include / --exclude rec-id filtering on --apply-to.

    Contract:
      - Both require --apply-to (pre-flight error).
      - Mutually exclusive (argparse mutex group).
      - Unknown ids → SystemExit listing all unknowns in one message.
      - Filter mutates plan.recommendations in place before apply_recommendations.
      - Empty effective plan is a clean exit-0 (no error).
    """

    @staticmethod
    def _seed_specs_dir(tmp_path):
        specs_dir = tmp_path / "specs"
        specs_dir.mkdir()
        (specs_dir / "fraud-detect.yaml").write_text(
            "metadata:\n  name: fraud-detect\n"
            "spec:\n  slos:\n    judgment:\n      target: 95.0\n"
        )
        return specs_dir

    @staticmethod
    def _build_single_rec_plan(tmp_path, *, incident: str = "inc-jmy24"):
        from nthlayer_workers.learn.recommendations import (
            SpecRecommendation, Recommendation,
        )
        from datetime import datetime, timezone

        plan = SpecRecommendation(
            incident=incident,
            generated_by="nthlayer-learn",
            generated_at=datetime(2026, 5, 29, tzinfo=timezone.utc),
            confidence=0.7,
            recommendations=[
                Recommendation(
                    id="rec-abc",
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
        return plan_in

    @staticmethod
    def _build_multi_rec_plan(tmp_path, *, incident: str = "inc-jmy24-multi"):
        """Build a plan with two recs on the same service so both can apply.

        Returns (plan_path, rec_abc_id, rec_def_id) — the rec ids are
        computed via compute_rec_id so callers can reference them by
        their deterministic ids.
        """
        from nthlayer_workers.learn.recommendations import (
            SpecRecommendation, Recommendation, compute_rec_id,
        )
        from datetime import datetime, timezone

        field_tighten = "spec.slos.judgment.target"
        field_gate = "spec.gates.judgment_quality"
        rec_abc_id = compute_rec_id(incident, "tighten_slo", field_tighten)
        rec_def_id = compute_rec_id(incident, "add_deploy_gate", field_gate)

        plan = SpecRecommendation(
            incident=incident,
            generated_by="nthlayer-learn",
            generated_at=datetime(2026, 5, 29, tzinfo=timezone.utc),
            confidence=0.7,
            recommendations=[
                Recommendation(
                    id=rec_abc_id,
                    service="fraud-detect",
                    type="tighten_slo",
                    rationale="test",
                    field=field_tighten,
                    current_value=95.0,
                    proposed_value=98.5,
                ),
                Recommendation(
                    id=rec_def_id,
                    service="fraud-detect",
                    type="add_deploy_gate",
                    rationale="test",
                    field=field_gate,
                    current_value=None,
                    proposed_value={"block_on": "judgment_quality"},
                ),
            ],
        )
        plan_in = tmp_path / "in.yaml"
        plan_in.write_text(plan.to_yaml())
        return plan_in, rec_abc_id, rec_def_id

    @staticmethod
    def _seed_multi_specs_dir(tmp_path):
        """Manifest with the SLO field present so tighten_slo can apply cleanly."""
        specs_dir = tmp_path / "specs"
        specs_dir.mkdir()
        (specs_dir / "fraud-detect.yaml").write_text(
            "metadata:\n  name: fraud-detect\n"
            "spec:\n"
            "  slos:\n    judgment:\n      target: 95.0\n"
        )
        return specs_dir

    def test_include_without_apply_to_rejected(self, tmp_path):
        from nthlayer_workers.learn.cli import main

        plan_in = self._build_single_rec_plan(tmp_path)
        with pytest.raises(SystemExit) as exc:
            main([
                "recommendations",
                "--from", str(plan_in),
                "--include", "rec-x",
            ])
        msg = str(exc.value.code) if exc.value.code is not None else ""
        assert "requires --apply-to" in msg

    def test_exclude_without_apply_to_rejected(self, tmp_path):
        from nthlayer_workers.learn.cli import main

        plan_in = self._build_single_rec_plan(tmp_path)
        with pytest.raises(SystemExit) as exc:
            main([
                "recommendations",
                "--from", str(plan_in),
                "--exclude", "rec-x",
            ])
        msg = str(exc.value.code) if exc.value.code is not None else ""
        assert "requires --apply-to" in msg

    def test_include_and_exclude_mutually_exclusive(self, tmp_path):
        from nthlayer_workers.learn.cli import main

        specs_dir = self._seed_specs_dir(tmp_path)
        plan_in = self._build_single_rec_plan(tmp_path)
        with pytest.raises(SystemExit):
            main([
                "recommendations",
                "--from", str(plan_in),
                "--apply-to", str(specs_dir),
                "--include", "rec-abc",
                "--exclude", "rec-def",
            ])

    def test_include_unknown_id_rejected(self, tmp_path, capsys):
        """jmy.24 P1 R5: unknown id → SystemExit(2) + stderr message."""
        from nthlayer_workers.learn.cli import main

        specs_dir = self._seed_specs_dir(tmp_path)
        plan_in = self._build_single_rec_plan(tmp_path)
        with pytest.raises(SystemExit) as exc:
            main([
                "recommendations",
                "--from", str(plan_in),
                "--apply-to", str(specs_dir),
                "--include", "rec-typo",
            ])
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "not found in plan" in err
        assert "rec-typo" in err

    def test_include_multiple_unknown_ids_reported_together(self, tmp_path, capsys):
        """jmy.24 P1 R5: stderr lists every unknown id in one message."""
        from nthlayer_workers.learn.cli import main

        specs_dir = self._seed_specs_dir(tmp_path)
        plan_in = self._build_single_rec_plan(tmp_path)
        with pytest.raises(SystemExit) as exc:
            main([
                "recommendations",
                "--from", str(plan_in),
                "--apply-to", str(specs_dir),
                "--include", "rec-typo1,rec-typo2",
            ])
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "rec-typo1" in err
        assert "rec-typo2" in err

    def test_include_duplicate_unknown_id_reported_once(self, tmp_path, capsys):
        """jmy.24 P1 R5: duplicate input ids are dedup'd in the error message."""
        from nthlayer_workers.learn.cli import main

        specs_dir = self._seed_specs_dir(tmp_path)
        plan_in = self._build_single_rec_plan(tmp_path)
        with pytest.raises(SystemExit) as exc:
            main([
                "recommendations",
                "--from", str(plan_in),
                "--apply-to", str(specs_dir),
                "--include", "rec-typo,rec-typo",
            ])
        assert exc.value.code == 2
        err = capsys.readouterr().err
        # "rec-typo" appears exactly once in the missing list.
        assert err.count("rec-typo") == 1

    def test_include_filters_plan_to_subset(self, tmp_path, capsys):
        """--include rec-abc only → JSON's applied contains only rec-abc."""
        import json
        from nthlayer_workers.learn.cli import main

        specs_dir = self._seed_multi_specs_dir(tmp_path)
        plan_in, rec_abc_id, rec_def_id = self._build_multi_rec_plan(tmp_path)

        # Use --json to inspect applied/skipped without parsing stderr text.
        main([
            "recommendations",
            "--from", str(plan_in),
            "--apply-to", str(specs_dir),
            "--include", rec_abc_id,
            "--json",
        ])

        out = capsys.readouterr().out
        doc = json.loads(out)
        all_ids = {e["id"] for e in doc["applied"]} | {e["id"] for e in doc["skipped"]}
        assert rec_abc_id in all_ids
        assert rec_def_id not in all_ids

    def test_exclude_filters_out_subset(self, tmp_path, capsys):
        import json
        from nthlayer_workers.learn.cli import main

        specs_dir = self._seed_multi_specs_dir(tmp_path)
        plan_in, rec_abc_id, rec_def_id = self._build_multi_rec_plan(tmp_path)

        try:
            main([
                "recommendations",
                "--from", str(plan_in),
                "--apply-to", str(specs_dir),
                "--exclude", rec_def_id,
                "--json",
            ])
        except SystemExit:
            # Acceptable: a non-zero exit can happen if anything else skipped.
            # We only care that rec_def_id was filtered out.
            pass

        out = capsys.readouterr().out
        doc = json.loads(out)
        all_ids = {e["id"] for e in doc["applied"]} | {e["id"] for e in doc["skipped"]}
        assert rec_abc_id in all_ids
        assert rec_def_id not in all_ids

    def test_exclude_all_results_in_empty_apply_exit_zero(self, tmp_path, capsys):
        """Excluding every rec → no error, format_summary shows Applied: 0."""
        from nthlayer_workers.learn.cli import main

        specs_dir = self._seed_multi_specs_dir(tmp_path)
        plan_in, rec_abc_id, rec_def_id = self._build_multi_rec_plan(tmp_path)

        # Should NOT raise — empty plan after filter is a clean no-op.
        main([
            "recommendations",
            "--from", str(plan_in),
            "--apply-to", str(specs_dir),
            "--exclude", f"{rec_abc_id},{rec_def_id}",
        ])

        captured = capsys.readouterr()
        assert "Applied: 0" in captured.err

    def test_include_with_json_mode_filters_doc(self, tmp_path, capsys):
        """--include rec-abc --json → JSON applied/skipped exclude filtered recs."""
        import json
        from nthlayer_workers.learn.cli import main

        specs_dir = self._seed_multi_specs_dir(tmp_path)
        plan_in, rec_abc_id, rec_def_id = self._build_multi_rec_plan(tmp_path)

        main([
            "recommendations",
            "--from", str(plan_in),
            "--apply-to", str(specs_dir),
            "--include", rec_abc_id,
            "--json",
        ])

        out = capsys.readouterr().out
        doc = json.loads(out)
        applied_ids = {e["id"] for e in doc["applied"]}
        skipped_ids = {e["id"] for e in doc["skipped"]}
        # The filtered-out rec is absent from both arrays.
        assert rec_def_id not in applied_ids
        assert rec_def_id not in skipped_ids
        # The kept rec is referenced (either applied or skipped — depends on
        # whether the manifest already matched; for tighten_slo with current
        # 95.0 and proposed 98.5 we expect applied).
        assert rec_abc_id in applied_ids
        assert skipped_ids == set()

    def test_no_filter_flags_applies_all(self, tmp_path, capsys):
        """Regression: without --include/--exclude, all plan recs flow through."""
        import json
        from nthlayer_workers.learn.cli import main

        specs_dir = self._seed_multi_specs_dir(tmp_path)
        plan_in, rec_abc_id, rec_def_id = self._build_multi_rec_plan(tmp_path)

        try:
            main([
                "recommendations",
                "--from", str(plan_in),
                "--apply-to", str(specs_dir),
                "--json",
            ])
        except SystemExit:
            # apply may exit non-zero if any rec skipped; the contract here
            # is purely that BOTH ids appear in the JSON.
            pass

        out = capsys.readouterr().out
        doc = json.loads(out)
        all_ids = {e["id"] for e in doc["applied"]} | {e["id"] for e in doc["skipped"]}
        assert rec_abc_id in all_ids
        assert rec_def_id in all_ids

    # ---- jmy.24 P3 R5 coverage gaps ----

    def test_output_snapshot_reflects_post_filter(self, tmp_path, capsys):
        """jmy.24 P3 R5: --output writes the post-filter plan so the
        snapshot reflects what was actually applied (GitOps pattern).
        """
        from nthlayer_workers.learn.cli import main
        from nthlayer_workers.learn.recommendations import parse_plan_file

        specs_dir = self._seed_multi_specs_dir(tmp_path)
        plan_in, rec_abc_id, _rec_def_id = self._build_multi_rec_plan(tmp_path)
        snapshot = tmp_path / "snapshot.yaml"

        try:
            main([
                "recommendations",
                "--from", str(plan_in),
                "--apply-to", str(specs_dir),
                "--output", str(snapshot),
                "--include", rec_abc_id,
            ])
        except SystemExit:
            pass

        saved = parse_plan_file(snapshot)
        saved_ids = {r.id for r in saved.recommendations}
        assert saved_ids == {rec_abc_id}

    def test_include_empty_string_is_silent_no_op(self, tmp_path, capsys):
        """jmy.24 P3 R5: --include "" is falsy → no filter applied.
        Regression-pin for the silent treatment so a future refactor
        doesn't accidentally start rejecting empty-string values.
        """
        import json
        from nthlayer_workers.learn.cli import main

        specs_dir = self._seed_multi_specs_dir(tmp_path)
        plan_in, rec_abc_id, rec_def_id = self._build_multi_rec_plan(tmp_path)

        try:
            main([
                "recommendations",
                "--from", str(plan_in),
                "--apply-to", str(specs_dir),
                "--include", "",
                "--json",
            ])
        except SystemExit:
            pass

        doc = json.loads(capsys.readouterr().out)
        all_ids = {e["id"] for e in doc["applied"]} | {e["id"] for e in doc["skipped"]}
        # Both recs present — no filter applied.
        assert rec_abc_id in all_ids
        assert rec_def_id in all_ids

    def test_include_commas_only_parses_to_empty_set(self, tmp_path, capsys):
        """jmy.24 P3 R5: --include "," strips to empty requested list →
        no missing ids (none requested), keep set is empty, plan filtered
        to []. Apply runs with empty plan, exits 0.
        """
        import json
        from nthlayer_workers.learn.cli import main

        specs_dir = self._seed_multi_specs_dir(tmp_path)
        plan_in, _rec_abc_id, _rec_def_id = self._build_multi_rec_plan(tmp_path)

        main([
            "recommendations",
            "--from", str(plan_in),
            "--apply-to", str(specs_dir),
            "--include", ",",
            "--json",
        ])

        doc = json.loads(capsys.readouterr().out)
        assert doc["applied"] == []
        assert doc["skipped"] == []
        assert doc["exit_code"] == 0

    def test_filter_preserves_original_rec_order(self, tmp_path, capsys):
        """jmy.24 P3 R5: filter result follows plan order, not arg order.
        Reversing the --include args list must NOT reverse the applied list.
        """
        import json
        from nthlayer_workers.learn.cli import main

        specs_dir = self._seed_multi_specs_dir(tmp_path)
        plan_in, rec_abc_id, rec_def_id = self._build_multi_rec_plan(tmp_path)
        # rec_abc_id appears first in the plan. Pass --include in reverse
        # order — the applied/skipped lists must still match plan order.
        try:
            main([
                "recommendations",
                "--from", str(plan_in),
                "--apply-to", str(specs_dir),
                "--include", f"{rec_def_id},{rec_abc_id}",
                "--json",
            ])
        except SystemExit:
            pass

        doc = json.loads(capsys.readouterr().out)
        all_ids = [e["id"] for e in doc["applied"]] + [e["id"] for e in doc["skipped"]]
        # Find each id's first index; rec_abc must come before rec_def
        # because the original plan order is [abc, def].
        assert all_ids.index(rec_abc_id) < all_ids.index(rec_def_id)

    def test_include_all_rec_ids_is_no_op_filter(self, tmp_path, capsys):
        """jmy.24 P3 R5: --include of every rec id is a no-op filter
        (every rec survives). Behaves identically to omitting the flag."""
        import json
        from nthlayer_workers.learn.cli import main

        specs_dir = self._seed_multi_specs_dir(tmp_path)
        plan_in, rec_abc_id, rec_def_id = self._build_multi_rec_plan(tmp_path)

        try:
            main([
                "recommendations",
                "--from", str(plan_in),
                "--apply-to", str(specs_dir),
                "--include", f"{rec_abc_id},{rec_def_id}",
                "--json",
            ])
        except SystemExit:
            pass

        doc = json.loads(capsys.readouterr().out)
        all_ids = {e["id"] for e in doc["applied"]} | {e["id"] for e in doc["skipped"]}
        assert all_ids == {rec_abc_id, rec_def_id}

    def test_include_against_zero_rec_plan_rejects_all_as_unknown(
        self, tmp_path, capsys,
    ):
        """jmy.24 P3 R5: plan with zero recommendations + any --include
        → every requested id is unknown → SystemExit(2).
        """
        from datetime import datetime, timezone
        from nthlayer_workers.learn.cli import main
        from nthlayer_workers.learn.recommendations import SpecRecommendation

        specs_dir = self._seed_specs_dir(tmp_path)
        empty_plan = SpecRecommendation(
            incident="inc-empty", generated_by="nthlayer-learn",
            generated_at=datetime(2026, 5, 29, tzinfo=timezone.utc),
            confidence=0.0, recommendations=[],
        )
        plan_in = tmp_path / "empty.yaml"
        plan_in.write_text(empty_plan.to_yaml())

        with pytest.raises(SystemExit) as exc:
            main([
                "recommendations",
                "--from", str(plan_in),
                "--apply-to", str(specs_dir),
                "--include", "rec-anything",
            ])
        assert exc.value.code == 2
        assert "not found in plan" in capsys.readouterr().err


class TestInteractiveFlag:
    """opensrm-jmy.22: --interactive launches the TUI walkthrough.

    Contract:
      - --interactive requires --apply-to or --output (pre-flight error).
      - --interactive and --json are mutually exclusive (pre-flight error).
      - After the --include/--exclude filter, dispatch calls
        run_walkthrough(plan, specs_dir=args.apply_to) from
        nthlayer_workers.learn._interactive_app; the returned plan flows
        into --output / --apply-to / --pr as the effective plan.
    """

    @staticmethod
    def _seed_specs_dir(tmp_path):
        specs_dir = tmp_path / "specs"
        specs_dir.mkdir()
        (specs_dir / "fraud-detect.yaml").write_text(
            "metadata:\n  name: fraud-detect\n"
            "spec:\n  slos:\n    judgment:\n      target: 95.0\n"
        )
        return specs_dir

    @staticmethod
    def _build_single_rec_plan(tmp_path, *, incident: str = "inc-jmy22"):
        from nthlayer_workers.learn.recommendations import (
            SpecRecommendation, Recommendation,
        )
        from datetime import datetime, timezone

        plan = SpecRecommendation(
            incident=incident,
            generated_by="nthlayer-learn",
            generated_at=datetime(2026, 5, 29, tzinfo=timezone.utc),
            confidence=0.7,
            recommendations=[
                Recommendation(
                    id="rec-aaaaaaaaaaaa",
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
        return plan_in

    @staticmethod
    def _build_multi_rec_plan(tmp_path, *, incident: str = "inc-jmy22-multi"):
        """Two recommendations with distinct rec ids ('rec-A' / 'rec-B')."""
        from nthlayer_workers.learn.recommendations import (
            SpecRecommendation, Recommendation,
        )
        from datetime import datetime, timezone

        plan = SpecRecommendation(
            incident=incident,
            generated_by="nthlayer-learn",
            generated_at=datetime(2026, 5, 29, tzinfo=timezone.utc),
            confidence=0.7,
            recommendations=[
                Recommendation(
                    id="rec-A",
                    service="fraud-detect",
                    type="tighten_slo",
                    rationale="test",
                    field="spec.slos.judgment.target",
                    current_value=95.0,
                    proposed_value=98.5,
                ),
                Recommendation(
                    id="rec-B",
                    service="fraud-detect",
                    type="add_deploy_gate",
                    rationale="test",
                    field="spec.gates.judgment_quality",
                    current_value=None,
                    proposed_value={"block_on": "judgment_quality"},
                ),
            ],
        )
        plan_in = tmp_path / "in.yaml"
        plan_in.write_text(plan.to_yaml())
        return plan_in

    def test_interactive_requires_apply_to_or_output(self, tmp_path):
        from nthlayer_workers.learn.cli import main

        plan_in = self._build_single_rec_plan(tmp_path)
        with pytest.raises(SystemExit) as exc:
            main([
                "recommendations",
                "--from", str(plan_in),
                "--interactive",
            ])
        msg = str(exc.value.code) if exc.value.code is not None else ""
        assert "requires --apply-to or --output" in msg

    def test_interactive_and_json_mutually_exclusive(self, tmp_path):
        from nthlayer_workers.learn.cli import main

        specs_dir = self._seed_specs_dir(tmp_path)
        plan_in = self._build_single_rec_plan(tmp_path)
        with pytest.raises(SystemExit) as exc:
            main([
                "recommendations",
                "--from", str(plan_in),
                "--apply-to", str(specs_dir),
                "--interactive",
                "--json",
            ])
        msg = str(exc.value.code) if exc.value.code is not None else ""
        assert "mutually exclusive" in msg

    @staticmethod
    def _install_fake_walkthrough(monkeypatch, fake_walkthrough):
        """Install a fake _interactive_app module to bypass textual import.

        cli.py does ``from ._interactive_app import run_walkthrough`` inside
        _cmd_recommendations. We replace the module in sys.modules so the
        lazy import inside the CLI returns our fake without ever loading
        textual.
        """
        import sys
        import types

        fake_module = types.ModuleType("nthlayer_workers.learn._interactive_app")
        fake_module.run_walkthrough = fake_walkthrough
        monkeypatch.setitem(
            sys.modules,
            "nthlayer_workers.learn._interactive_app",
            fake_module,
        )

    def test_interactive_with_apply_to_runs_walkthrough(
        self, tmp_path, monkeypatch,
    ):
        """Stub run_walkthrough → empty plan; no recs applied to manifest."""
        from nthlayer_workers.learn.cli import main
        from nthlayer_workers.learn.recommendations import SpecRecommendation
        from datetime import datetime, timezone

        specs_dir = self._seed_specs_dir(tmp_path)
        plan_in = self._build_single_rec_plan(tmp_path)
        manifest_before = (specs_dir / "fraud-detect.yaml").read_text()

        def fake_walkthrough(plan, specs_dir=None):
            # Operator "rejected all": return a plan with zero recs.
            return SpecRecommendation(
                incident=plan.incident,
                generated_by=plan.generated_by,
                generated_at=datetime(2026, 5, 29, tzinfo=timezone.utc),
                confidence=0.0,
                recommendations=[],
            )

        self._install_fake_walkthrough(monkeypatch, fake_walkthrough)

        # Empty effective plan → clean no-op exit 0.
        main([
            "recommendations",
            "--from", str(plan_in),
            "--apply-to", str(specs_dir),
            "--interactive",
        ])

        # Manifest unchanged.
        assert (specs_dir / "fraud-detect.yaml").read_text() == manifest_before

    def test_interactive_with_output_writes_post_walkthrough_plan(
        self, tmp_path, monkeypatch,
    ):
        """Stub run_walkthrough → 1-rec plan; --output reflects that plan."""
        from nthlayer_workers.learn.cli import main
        from nthlayer_workers.learn.recommendations import (
            SpecRecommendation, Recommendation, parse_plan_file,
        )
        from datetime import datetime, timezone

        plan_in = self._build_single_rec_plan(tmp_path)
        out_path = tmp_path / "post.yaml"

        def fake_walkthrough(plan, specs_dir=None):
            return SpecRecommendation(
                incident=plan.incident,
                generated_by=plan.generated_by,
                generated_at=datetime(2026, 5, 29, tzinfo=timezone.utc),
                confidence=0.7,
                recommendations=[
                    Recommendation(
                        id="rec-postwalk",
                        service="fraud-detect",
                        type="tighten_slo",
                        rationale="kept by operator",
                        field="spec.slos.judgment.target",
                        current_value=95.0,
                        proposed_value=99.5,
                    ),
                ],
            )

        self._install_fake_walkthrough(monkeypatch, fake_walkthrough)

        main([
            "recommendations",
            "--from", str(plan_in),
            "--output", str(out_path),
            "--interactive",
        ])

        saved = parse_plan_file(out_path)
        assert len(saved.recommendations) == 1
        assert saved.recommendations[0].id == "rec-postwalk"
        assert saved.recommendations[0].proposed_value == 99.5

    def test_interactive_with_include_passes_post_filter_plan_to_walkthrough(
        self, tmp_path, monkeypatch,
    ):
        """--include rec-A runs before walkthrough; rec-B is filtered out."""
        from nthlayer_workers.learn.cli import main
        from nthlayer_workers.learn.recommendations import SpecRecommendation
        from datetime import datetime, timezone

        specs_dir = self._seed_specs_dir(tmp_path)
        plan_in = self._build_multi_rec_plan(tmp_path)
        captured: dict = {}

        def fake_walkthrough(plan, specs_dir=None):
            captured["plan"] = plan
            # Return empty so apply is a clean no-op.
            return SpecRecommendation(
                incident=plan.incident,
                generated_by=plan.generated_by,
                generated_at=datetime(2026, 5, 29, tzinfo=timezone.utc),
                confidence=0.0,
                recommendations=[],
            )

        self._install_fake_walkthrough(monkeypatch, fake_walkthrough)

        main([
            "recommendations",
            "--from", str(plan_in),
            "--apply-to", str(specs_dir),
            "--include", "rec-A",
            "--interactive",
        ])

        assert "plan" in captured
        captured_ids = [r.id for r in captured["plan"].recommendations]
        assert captured_ids == ["rec-A"]
