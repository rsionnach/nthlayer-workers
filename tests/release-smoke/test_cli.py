"""Smoke test: console-script entry point resolves and runs `--help`
without crashing.

Catches:
  - ``[project.scripts]`` entry point misconfigured in pyproject.toml
  - import-time crash in ``nthlayer_bench.cli`` (e.g. missing optional
    dep that the wheel didn't declare as required)
  - renamed ``main`` function the entry point can't find
"""
from __future__ import annotations

import shutil
import subprocess

CONSOLE_SCRIPT = "nthlayer-workers"


def test_console_script_on_path():
    assert shutil.which(CONSOLE_SCRIPT), (
        f"{CONSOLE_SCRIPT} not on PATH after wheel install — "
        "[project.scripts] entry point likely misconfigured"
    )


def test_help_runs_clean():
    result = subprocess.run(
        [CONSOLE_SCRIPT, "--help"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, (
        f"`{CONSOLE_SCRIPT} --help` exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert result.stdout.strip(), (
        f"`{CONSOLE_SCRIPT} --help` produced no stdout — "
        "argparse/click likely didn't render help text"
    )
