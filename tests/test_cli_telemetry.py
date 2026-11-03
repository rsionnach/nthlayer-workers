"""Every CLI entry point initialises telemetry before it runs a command.

Without ``init_telemetry`` no LoggerProvider is installed, and every event a
command emits is dropped (opensrm-quqo). The ``python -m`` entry points call
these same ``main`` functions.
"""

import importlib
import sys
from unittest.mock import patch

import pytest

from nthlayer_workers import TELEMETRY_SERVICE_NAME


class _Initialised(Exception):
    """Raised by the patched init_telemetry, so the test stops before the
    command does anything real, and proves the call came first."""


# Each argv is one the CLI's parser accepts, so main gets past parse_args.
ENTRY_POINTS = [
    ("nthlayer_workers.cli", []),
    ("nthlayer_workers.measure.cli", []),
    ("nthlayer_workers.observe.cli", ["drift", "--service", "s", "--prometheus-url", "http://p"]),
    ("nthlayer_workers.learn.cli", ["accuracy", "--producer", "p"]),
    ("nthlayer_workers.respond.cli", []),
    ("nthlayer_workers.correlate.cli", []),
]


@pytest.mark.parametrize(("module", "argv"), ENTRY_POINTS, ids=[m for m, _ in ENTRY_POINTS])
def test_main_initialises_telemetry_before_running_a_command(module, argv):
    cli = importlib.import_module(module)
    with patch.object(cli, "init_telemetry", side_effect=_Initialised) as init, \
         patch.object(sys, "argv", ["prog", *argv]), \
         pytest.raises(_Initialised):
        cli.main()

    init.assert_called_once_with(TELEMETRY_SERVICE_NAME)


def test_every_entry_point_is_covered():
    """A new console script, or a new ``python -m`` module, must have its main
    added to ENTRY_POINTS."""
    import tomllib
    from pathlib import Path

    root = Path(__file__).parents[1]
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    scripts = {target.split(":")[0] for target in pyproject["project"]["scripts"].values()}
    # Each __main__ module runs its sibling cli.main.
    mains = [p.parent for p in (root / "src" / "nthlayer_workers").rglob("__main__.py")]
    assert len(mains) >= 4, "__main__ discovery found too few modules; the layout moved"
    via_main = {f"nthlayer_workers.{pkg.name}.cli" for pkg in mains}

    assert scripts | via_main <= {m for m, _ in ENTRY_POINTS}


def test_the_otel_sdk_and_log_exporter_are_installed():
    """The [otel] extra must actually deliver what init_telemetry imports, or
    it reports SDK_MISSING and every event drops."""
    import opentelemetry.exporter.otlp.proto.grpc._log_exporter  # noqa: F401
    import opentelemetry.sdk._logs  # noqa: F401


def test_the_init_status_is_logged(caplog):
    """An operator can tell from the debug log whether events export."""
    import logging

    from nthlayer_common.telemetry import TelemetryStatus

    from nthlayer_workers import log_telemetry_status

    with caplog.at_level(logging.DEBUG, logger="nthlayer_workers"):
        log_telemetry_status(TelemetryStatus.EXTERNAL_PROVIDER)

    (record,) = [r for r in caplog.records if r.name == "nthlayer_workers"]
    assert record.args == ("external_provider",)
