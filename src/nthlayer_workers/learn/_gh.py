"""GitHub + git integration via gh CLI for the Learn → Spec workflow (jmy.6).

All subprocess calls go through this module so tests can monkeypatch
at a single boundary. Pluggable hosting is filed as a v2 follow-up;
this file is named with leading underscore signalling it's internal
and replaceable.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class PreflightError(RuntimeError):
    """Raised when a pre-flight check fails.

    Carries a reason code in the message (gh_not_installed,
    gh_not_authenticated, not_a_git_repo, no_remote, branch_exists)
    so CLI can format an actionable error.
    """


@dataclass(frozen=True)
class PRResult:
    """Outcome of create_pr_via_gh."""
    url: str | None
    number: int | None
    error: str | None

    @property
    def ok(self) -> bool:
        return self.url is not None and self.error is None


def check_gh_installed() -> None:
    """Verify gh CLI is on PATH. Raises PreflightError(gh_not_installed)."""
    try:
        result = subprocess.run(
            ["gh", "--version"], capture_output=True, text=True, check=False,
        )
    except FileNotFoundError as exc:
        raise PreflightError(
            "gh_not_installed: gh CLI not found on PATH. "
            "Install via 'brew install gh' or see https://cli.github.com"
        ) from exc
    if result.returncode != 0:
        raise PreflightError(
            f"gh_not_installed: gh --version exited {result.returncode}"
        )


def check_gh_auth() -> None:
    """Verify gh is authenticated. Raises PreflightError(gh_not_authenticated)."""
    result = subprocess.run(
        ["gh", "auth", "status"], capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise PreflightError(
            "gh_not_authenticated: gh CLI not logged in. Run 'gh auth login'."
        )


def check_git_repo(specs_dir: Path) -> None:
    """Verify specs_dir is inside a git repo."""
    result = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=specs_dir, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise PreflightError(
            f"not_a_git_repo: {specs_dir} is not inside a git repository"
        )


def check_remote(specs_dir: Path) -> None:
    """Verify the repo has an 'origin' remote."""
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=specs_dir, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise PreflightError(
            "no_remote: 'origin' remote not configured. "
            "Add via 'git remote add origin <url>'."
        )


def check_branch_available(specs_dir: Path, branch: str) -> None:
    """Verify the branch name is not already in use (local OR remote)."""
    # Local
    local = subprocess.run(
        ["git", "show-ref", "--verify", f"refs/heads/{branch}"],
        cwd=specs_dir, capture_output=True, text=True, check=False,
    )
    if local.returncode == 0:
        raise PreflightError(
            f"branch_exists: branch {branch!r} already exists locally. "
            f"Delete with 'git branch -D {branch}' or rename."
        )

    # Remote
    remote = subprocess.run(
        ["git", "ls-remote", "--heads", "origin", branch],
        cwd=specs_dir, capture_output=True, text=True, check=False,
    )
    if remote.returncode == 0 and remote.stdout.strip():
        raise PreflightError(
            f"branch_exists: branch {branch!r} already exists on origin. "
            f"Delete with 'git push origin --delete {branch}'."
        )
