"""A manifest file set aside by the directory scan reaches the operator.

``svc.yaml`` beside ``svc.yml``: nthlayer-common's ``scan_manifest_files``
reads the ``.yaml`` and returns the other as a ``SuffixCollision`` (stem,
kept, dropped). The observe and measure loaders carry those records beside
their parse-failure count, and their CLIs print them on stderr, on every run
(opensrm-j9wq). The learn side is in
tests/learn/test_retrospective_manifest_failures.py.

The collision records here come from the real scan over real files in
``tmp_path``, never hand-built, so they have the shape common produces.
"""

from __future__ import annotations

import argparse
import textwrap
import warnings
from pathlib import Path
from unittest.mock import patch

import pytest
import structlog
from nthlayer_common.manifest import SuffixCollision, scan_manifest_files

from nthlayer_workers import cli_output
from nthlayer_workers.measure import cli as measure_cli
from nthlayer_workers.measure.adapters import prometheus as measure_adapter
from nthlayer_workers.observe import cli as observe_cli
from nthlayer_workers.observe.slo import spec_loader

# srm/v1 with one SLO, the mapping shape of the shipped
# nthlayer/demo/specs/fraud-detect.yaml. An SLO is needed so that a service
# missing from the view shows as SLOs missing from the result.
MANIFEST = textwrap.dedent("""\
    apiVersion: srm/v1
    kind: ServiceReliabilityManifest
    metadata:
      name: payments
      team: payments
      tier: critical
    spec:
      type: api
      slos:
        availability:
          target: 99.9
          window: 30d
""")
# The same document declaring a DIFFERENT service: what makes a collision
# more than a duplicate.
OTHER_MANIFEST = MANIFEST.replace("name: payments", "name: payments-api")


@pytest.fixture
def colliding_dir(tmp_path: Path) -> Path:
    """``payments.yaml`` (service payments) beside ``payments.yml`` (service
    payments-api), plus an unrelated ``orders.yaml``."""
    (tmp_path / "payments.yaml").write_text(MANIFEST)
    (tmp_path / "payments.yml").write_text(OTHER_MANIFEST)
    (tmp_path / "orders.yaml").write_text(MANIFEST.replace("name: payments", "name: orders"))
    return tmp_path


@pytest.fixture
def clean_dir(tmp_path: Path) -> Path:
    (tmp_path / "payments.yaml").write_text(MANIFEST)
    return tmp_path


def _expected(directory: Path) -> tuple[SuffixCollision, ...]:
    return (
        SuffixCollision(
            stem="payments",
            kept=directory / "payments.yaml",
            dropped=directory / "payments.yml",
        ),
    )


def test_the_fixture_is_a_collision_as_common_sees_it(colliding_dir: Path):
    """Guards every test below against a fixture that stopped colliding: if
    common changed what it sets aside, the rest would pass on empty tuples."""
    assert tuple(scan_manifest_files(colliding_dir).suffix_collisions) == _expected(colliding_dir)


# --- observe ---------------------------------------------------------------


def test_observe_load_specs_returns_the_collision(colliding_dir: Path):
    loaded = spec_loader.load_specs(colliding_dir)

    assert loaded.suffix_collisions == _expected(colliding_dir)
    # The service in the file set aside is absent, not merged in.
    assert sorted({s.service for s in loaded.service_slos}) == ["orders", "payments"]
    assert loaded.parse_failures == 0


def test_observe_logs_the_collision_on_every_scan(colliding_dir: Path):
    with structlog.testing.capture_logs() as logs:
        for _ in range(3):
            spec_loader.load_specs(colliding_dir)

    events = [e for e in logs if e["event"] == "spec_suffix_collision"]
    assert [(e["log_level"], e["kept"], e["dropped"]) for e in events] == [
        ("warning", str(colliding_dir / "payments.yaml"), str(colliding_dir / "payments.yml"))
    ] * 3


def test_observe_clean_directory_has_no_collisions(clean_dir: Path):
    with structlog.testing.capture_logs() as logs:
        loaded = spec_loader.load_specs(clean_dir)

    assert loaded.suffix_collisions == ()
    assert [e for e in logs if e["event"] == "spec_suffix_collision"] == []


# --- measure ---------------------------------------------------------------


def test_measure_load_specs_returns_the_collision(colliding_dir: Path):
    loaded = measure_adapter.load_specs(colliding_dir)

    assert loaded.suffix_collisions == _expected(colliding_dir)
    assert sorted({s.service for s in loaded.slos}) == ["orders", "payments"]
    assert loaded.parse_failures == 0


def test_measure_logs_the_collision_on_every_scan(colliding_dir: Path, caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger=measure_adapter.logger.name):
        for _ in range(3):
            measure_adapter.load_specs(colliding_dir)

    # The record's args, not its rendered text: (dropped path, kept name).
    args = [
        r.args for r in caplog.records
        if r.name == measure_adapter.logger.name and r.levelno == logging.WARNING
    ]
    assert args == [(colliding_dir / "payments.yml", "payments.yaml")] * 3


def test_measure_clean_and_missing_directories_have_no_collisions(clean_dir: Path):
    assert measure_adapter.load_specs(clean_dir).suffix_collisions == ()
    assert measure_adapter.load_specs(clean_dir / "absent").suffix_collisions == ()


@pytest.mark.parametrize(
    "load",
    [spec_loader.load_specs, measure_adapter.load_specs],
    ids=["observe", "measure"],
)
def test_neither_loader_depends_on_the_python_warning(load, colliding_dir: Path):
    """Acceptance: no consumer relies on ManifestCollisionWarning. With every
    warning an error, the scan still completes and reports the collision."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        loaded = load(colliding_dir)

    assert loaded.suffix_collisions == _expected(colliding_dir)


# --- what the operator is told ---------------------------------------------


def test_each_collision_gets_one_line_that_leads_with_the_file_not_read(colliding_dir: Path):
    collisions = _expected(colliding_dir)

    lines = cli_output.suffix_collision_lines(collisions)

    # Anchored at the start, not by containment: the line names both files,
    # so "payments.yml in line" would pass with the two swapped.
    assert len(lines) == 1
    assert lines[0].startswith(f"Warning: {colliding_dir / 'payments.yml'} was not read: payments.yaml ")


def test_no_collisions_means_no_lines():
    assert cli_output.suffix_collision_lines(()) == []


def _captured_warnings():
    """A stand-in for ``warn_suffix_collisions`` that records what it was
    given and still calls the real one, and the list it records into. Each
    test patches it in where the CLI under test looks the function up."""
    seen: list[tuple[SuffixCollision, ...]] = []
    real = cli_output.warn_suffix_collisions

    def capture(collisions):
        seen.append(tuple(collisions))
        real(collisions)

    return seen, capture


def test_measure_evaluate_once_warns_about_the_collision(colliding_dir: Path, tmp_path_factory):
    """evaluate-once end to end: the collision the loader found is what the
    CLI hands to the operator warning. Prometheus answers nothing, so the run
    writes no verdict and exits normally."""
    seen, capture = _captured_warnings()
    args = argparse.Namespace(
        specs_dir=str(colliding_dir), hysteresis=3,
        verdict_store=str(tmp_path_factory.mktemp("store") / "v.db"),
        prometheus_url="http://prom", decision_store=None,
    )

    with patch(
        "nthlayer_workers.measure.adapters.prometheus.query_prometheus",
        return_value=None,
    ), patch("nthlayer_workers.cli_output.warn_suffix_collisions", side_effect=capture):
        measure_cli.cmd_evaluate_once(args)

    assert seen == [_expected(colliding_dir)]


@pytest.mark.parametrize("command", ["_cmd_collect", "_cmd_verify"])
def test_observe_commands_warn_about_the_collision(command: str, tmp_path: Path):
    """Both observe commands that scan a directory. The manifests declare no
    SLO, so each returns before it needs Prometheus: what is under test is
    that the scan's collision reaches the operator warning first."""
    no_slo = MANIFEST[: MANIFEST.index("  slos:")]
    (tmp_path / "payments.yaml").write_text(no_slo)
    (tmp_path / "payments.yml").write_text(no_slo.replace("name: payments", "name: payments-api"))
    seen, capture = _captured_warnings()

    with patch.object(observe_cli, "warn_suffix_collisions", side_effect=capture):
        exit_code = getattr(observe_cli, command)(argparse.Namespace(specs_dir=str(tmp_path)))

    assert exit_code == 0
    assert seen == [_expected(tmp_path)]
