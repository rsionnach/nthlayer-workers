"""`nthlayer-workers serve` flags reach the modules they configure."""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from nthlayer_workers import cli


class _RecordingRunner:
    """Stands in for ModuleRunner: keeps what is registered, runs nothing."""

    instances: list[_RecordingRunner] = []

    def __init__(self, *args, **kwargs) -> None:
        self.modules: dict[str, object] = {}
        self.intervals: dict[str, float] = {}
        _RecordingRunner.instances.append(self)

    def register(self, module, interval_seconds) -> None:
        self.modules[module.name] = module
        self.intervals[module.name] = interval_seconds

    async def run(self) -> None:
        return None


def _serve(*flags: str) -> _RecordingRunner:
    _RecordingRunner.instances.clear()
    with patch.object(sys, "argv", ["nthlayer-workers", "serve", *flags]), \
         patch.object(cli, "ModuleRunner", _RecordingRunner), \
         patch.object(cli, "init_telemetry"):
        cli.main()
    (runner,) = _RecordingRunner.instances
    return runner


def test_the_retrospective_flags_default_to_ten_minutes_and_a_day():
    retro = _serve().modules["learn.retrospective"]

    assert (retro.quiet_minutes, retro.max_age_hours) == (10, 24)


def test_the_retrospective_flags_reach_the_module():
    runner = _serve(
        "--retrospective-quiet-minutes", "2.5", "--retrospective-max-age-hours", "6",
        "--retrospective-interval", "45",
    )

    retro = runner.modules["learn.retrospective"]
    assert (retro.quiet_minutes, retro.max_age_hours) == (2.5, 6.0)
    assert runner.intervals["learn.retrospective"] == 45


@pytest.mark.parametrize("flag", ["--retrospective-quiet-minutes", "--retrospective-max-age-hours"])
def test_a_retrospective_flag_must_be_a_number(flag):
    with pytest.raises(SystemExit) as exc_info:
        _serve(flag, "soon")

    assert exc_info.value.code == 2
