"""Tests for the collect CLI command."""

import pytest

from nthlayer_workers.observe.cli import main


class TestCollectCLI:
    def test_collect_help(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(["collect", "--help"])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "--specs-dir" in captured.out
        assert "--prometheus-url" in captured.out

    def test_collect_missing_required_args(self):
        with pytest.raises(SystemExit) as exc_info:
            main(["collect"])
        assert exc_info.value.code == 2

    def test_collect_empty_specs_dir(self, tmp_path, capsys):
        result = main([
            "collect",
            "--specs-dir", str(tmp_path),
            "--prometheus-url", "http://localhost:9090",
        ])
        assert result == 0
        captured = capsys.readouterr()
        assert "No SLO definitions found" in captured.err
