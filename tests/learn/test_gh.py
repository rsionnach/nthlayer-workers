"""Unit tests for nthlayer_workers.learn._gh (jmy.6)."""
from __future__ import annotations

import subprocess
import pytest


class TestPreflightChecks:
    """gh + git pre-flight checks per jmy.6 design § 7 Category A."""

    def test_check_gh_installed_raises_when_missing(self, monkeypatch):
        from nthlayer_workers.learn._gh import check_gh_installed, PreflightError

        def fake_run(*args, **kwargs):
            raise FileNotFoundError("no such file")
        monkeypatch.setattr(subprocess, "run", fake_run)

        with pytest.raises(PreflightError, match="gh_not_installed"):
            check_gh_installed()

    def test_check_gh_installed_passes_when_present(self, monkeypatch):
        from nthlayer_workers.learn._gh import check_gh_installed

        result = subprocess.CompletedProcess(
            args=["gh", "--version"], returncode=0, stdout="gh version 2.x\n", stderr="",
        )
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: result)

        check_gh_installed()  # no raise

    def test_check_gh_auth_raises_on_not_authenticated(self, monkeypatch):
        from nthlayer_workers.learn._gh import check_gh_auth, PreflightError

        result = subprocess.CompletedProcess(
            args=["gh", "auth", "status"], returncode=1, stdout="", stderr="not logged in",
        )
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: result)

        with pytest.raises(PreflightError, match="gh_not_authenticated"):
            check_gh_auth()

    def test_check_git_repo_raises_outside_repo(self, monkeypatch, tmp_path):
        from nthlayer_workers.learn._gh import check_git_repo, PreflightError

        result = subprocess.CompletedProcess(
            args=["git", "rev-parse", "--git-dir"], returncode=128, stdout="", stderr="fatal",
        )
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: result)

        with pytest.raises(PreflightError, match="not_a_git_repo"):
            check_git_repo(tmp_path)

    def test_check_remote_raises_when_no_origin(self, monkeypatch, tmp_path):
        from nthlayer_workers.learn._gh import check_remote, PreflightError

        result = subprocess.CompletedProcess(
            args=["git", "remote", "get-url", "origin"], returncode=128, stdout="", stderr="",
        )
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: result)

        with pytest.raises(PreflightError, match="no_remote"):
            check_remote(tmp_path)

    def test_check_branch_available_raises_when_local_exists(self, monkeypatch, tmp_path):
        from nthlayer_workers.learn._gh import check_branch_available, PreflightError

        def fake_run(args, **kwargs):
            if "show-ref" in args:
                # Local branch exists
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="ref\n", stderr="")
            # Remote check (won't be reached if local check raises)
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)

        with pytest.raises(PreflightError, match="branch_exists"):
            check_branch_available(tmp_path, "learn/recommendations/inc-test")

    def test_check_branch_available_raises_when_remote_exists(self, monkeypatch, tmp_path):
        from nthlayer_workers.learn._gh import check_branch_available, PreflightError

        def fake_run(args, **kwargs):
            if "show-ref" in args:
                # Local doesn't exist
                return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="")
            if "ls-remote" in args:
                # Remote branch exists
                return subprocess.CompletedProcess(
                    args=args, returncode=0,
                    stdout="abc123\trefs/heads/learn/recommendations/inc-test\n",
                    stderr="",
                )
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)

        with pytest.raises(PreflightError, match="branch_exists"):
            check_branch_available(tmp_path, "learn/recommendations/inc-test")
