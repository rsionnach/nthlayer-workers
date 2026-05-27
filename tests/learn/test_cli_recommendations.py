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
