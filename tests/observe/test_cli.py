"""Tests for nthlayer_observe.cli module."""

import pytest

from nthlayer_workers.observe.cli import main


class TestCLI:
    def test_help_exits_zero(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(["--help"])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "nthlayer-observe" in captured.out
        assert "collect" in captured.out

    def test_no_args_exits_error(self):
        with pytest.raises(SystemExit) as exc_info:
            main([])
        assert exc_info.value.code == 2

    def test_collect_requires_args(self):
        """collect now requires --specs-dir and --prometheus-url."""
        with pytest.raises(SystemExit) as exc_info:
            main(["collect"])
        assert exc_info.value.code == 2

    def test_drift_requires_args(self):
        """drift now requires --service and --prometheus-url."""
        with pytest.raises(SystemExit) as exc_info:
            main(["drift"])
        assert exc_info.value.code == 2

    def test_verify_requires_args(self):
        """verify now requires --specs-dir and --prometheus-url."""
        with pytest.raises(SystemExit) as exc_info:
            main(["verify"])
        assert exc_info.value.code == 2

    def test_discover_requires_args(self):
        """discover now requires --prometheus-url."""
        with pytest.raises(SystemExit) as exc_info:
            main(["discover"])
        assert exc_info.value.code == 2

    def test_dependencies_requires_args(self):
        """dependencies requires --service."""
        with pytest.raises(SystemExit) as exc_info:
            main(["dependencies"])
        assert exc_info.value.code == 2

    def test_blast_radius_requires_args(self):
        """blast-radius requires --service."""
        with pytest.raises(SystemExit) as exc_info:
            main(["blast-radius"])
        assert exc_info.value.code == 2

    def test_check_deploy_requires_args(self):
        """check-deploy now requires --service."""
        with pytest.raises(SystemExit) as exc_info:
            main(["check-deploy"])
        assert exc_info.value.code == 2


class TestPackage:
    def test_version_importable(self):
        from nthlayer_workers.observe import __version__

        assert __version__ == "0.1.0"

    def test_nthlayer_common_importable(self):
        from nthlayer_common import NthLayerError

        assert issubclass(NthLayerError, Exception)

    def test_config_defaults(self):
        from nthlayer_workers.observe.config import ObserveConfig

        config = ObserveConfig()
        assert config.prometheus_url == "http://localhost:9090"
        assert config.store_path == "assessments.db"
