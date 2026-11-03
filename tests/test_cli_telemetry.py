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


def test_every_console_script_is_covered():
    """A new console script must be added to ENTRY_POINTS."""
    import tomllib
    from pathlib import Path

    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    scripts = {target.split(":")[0] for target in pyproject["project"]["scripts"].values()}
    assert scripts <= {m for m, _ in ENTRY_POINTS}
