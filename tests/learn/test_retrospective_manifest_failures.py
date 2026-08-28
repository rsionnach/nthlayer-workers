"""Manifest parse failures are logged and counted (opensrm-oh27).

``_load_manifests_from_specs`` used to swallow ``ManifestLoadError`` with a
bare ``continue`` — no log, no counter, no re-raise. Because the retrospective
computes financial impact per service, that did not produce a questionable
number; it produced a *confident* number computed over a subset, with the
dropped service indistinguishable from one that genuinely had no impact.

The loop's very next branch already logs when it skips a duplicate, so the
convention was established and only the parse-failure branch ignored it.
These tests pin the count at every surface it has to survive:
``_load_manifests_from_specs`` (the warning log carrying file path and error,
plus ``LoadedManifests.parse_failures``), ``build_retrospective``
(``metadata.custom["manifest_parse_failures"]``), and the ``nthlayer-learn
retrospective`` CLI, which is where a person actually reads the number the
skip was quietly shrinking.
"""
from __future__ import annotations

import argparse
import textwrap
from pathlib import Path

import pytest
import structlog
from nthlayer_common.verdicts.core import create
from nthlayer_common.verdicts.models import Verdict
from nthlayer_common.verdicts.sqlite_store import SQLiteVerdictStore
from nthlayer_common.verdicts.store import MemoryStore

from nthlayer_workers.learn.cli import _cmd_retrospective
from nthlayer_workers.learn.retrospective import (
    _load_manifests_from_specs,
    build_retrospective,
)

GOOD_MANIFEST = textwrap.dedent("""
    apiVersion: opensrm.nthlayer.io/v2
    kind: ServiceManifest
    metadata: {name: svc-good, labels: {tier: critical}}
    spec:
      owner: {group: group:default/team-a}
      service: {name: svc-good, type: api}
""").strip()

# Unparseable: spec.owner.group is not a valid Backstage entity reference,
# so load_manifest raises ManifestLoadError rather than a YAML error.
BROKEN_MANIFEST = textwrap.dedent("""
    apiVersion: opensrm.nthlayer.io/v2
    kind: ServiceManifest
    metadata: {name: svc-broken, labels: {tier: critical}}
    spec:
      owner: {group: not-an-entity-ref}
      service: {name: svc-broken, type: api}
""").strip()


def _write_specs(specs_dir: Path, *, broken: int = 0) -> Path:
    """Write one parseable manifest plus ``broken`` unparseable ones."""
    specs_dir.mkdir(parents=True, exist_ok=True)
    (specs_dir / "svc-good.yaml").write_text(GOOD_MANIFEST)
    for i in range(broken):
        (specs_dir / f"svc-broken-{i}.yaml").write_text(
            BROKEN_MANIFEST.replace("svc-broken", f"svc-broken-{i}")
        )
    return specs_dir


def _incident() -> Verdict:
    """An incident verdict with no lineage — the minimum ``build_retrospective``
    accepts. ``subject.type='custom'`` because nthlayer-common's
    VALID_SUBJECT_TYPES has no 'incident' bucket and build_retrospective does
    not inspect the incident's own subject.type.
    """
    return create(
        subject={
            "type": "custom",
            "ref": "INC-1",
            "service": "svc-good",
            "summary": "test incident",
        },
        judgment={"action": "flag", "confidence": 0.9, "reasoning": "test"},
        producer={"system": "test"},
        metadata={"custom": {"incident_id": "INC-1"}},
    )


@pytest.fixture
def incident_db(tmp_path: Path) -> tuple[Path, str]:
    """``(db_path, incident_verdict_id)`` for a real on-disk store holding one
    incident. The CLI path opens the DB by path, so a MemoryStore will not do
    (CLAUDE.md rule 14).
    """
    db = tmp_path / "verdicts.db"
    store = SQLiteVerdictStore(str(db))
    incident = _incident()
    store.put(incident)
    store.close()
    return db, incident.id


class TestParseFailureLogged:
    """The skip leaves a trace: file path and error, at warning level."""

    def test_parse_failure_logs_spec_file_and_error(self, tmp_path: Path):
        _write_specs(tmp_path, broken=1)

        with structlog.testing.capture_logs() as logs:
            _load_manifests_from_specs(str(tmp_path))

        failures = [e for e in logs if e["event"] == "manifest_parse_failed"]
        assert len(failures) == 1
        assert failures[0]["log_level"] == "warning"
        assert failures[0]["spec_file"].endswith("svc-broken-0.yaml")
        assert failures[0]["error"]

    def test_clean_specs_dir_logs_no_parse_failure(self, tmp_path: Path):
        _write_specs(tmp_path)

        with structlog.testing.capture_logs() as logs:
            _load_manifests_from_specs(str(tmp_path))

        assert [e for e in logs if e["event"] == "manifest_parse_failed"] == []


class TestParseFailureCount:
    """The count reaches the caller on the result object."""

    def test_count_reflects_unparseable_files(self, tmp_path: Path):
        _write_specs(tmp_path, broken=2)

        loaded = _load_manifests_from_specs(str(tmp_path))

        assert loaded.parse_failures == 2
        assert set(loaded.manifests) == {"svc-good"}

    def test_count_is_zero_for_clean_specs_dir(self, tmp_path: Path):
        _write_specs(tmp_path)

        loaded = _load_manifests_from_specs(str(tmp_path))

        assert loaded.parse_failures == 0
        assert set(loaded.manifests) == {"svc-good"}

    def test_count_is_zero_when_specs_dir_absent(self):
        loaded = _load_manifests_from_specs(None)

        assert loaded.parse_failures == 0
        assert loaded.manifests == {}


class TestRetrospectiveSurfacesParseFailures:
    """``build_retrospective`` carries the count so consumers of the
    financial figure can tell "computed over everything" from "computed
    over a subset". The key is always present — an absent key would be
    indistinguishable from a clean run.
    """

    def test_parse_failure_count_present_in_retro_custom(self, tmp_path: Path):
        _write_specs(tmp_path, broken=1)
        store = MemoryStore()
        incident = _incident()
        store.put(incident)

        retro = build_retrospective(incident.id, store, specs_dir=str(tmp_path))

        assert retro.metadata.custom["manifest_parse_failures"] == 1

    def test_parse_failure_count_zero_when_no_specs_dir(self):
        store = MemoryStore()
        incident = _incident()
        store.put(incident)

        retro = build_retrospective(incident.id, store)

        assert retro.metadata.custom["manifest_parse_failures"] == 0


class TestCliSurfacesParseFailures:
    """The count reaching ``retro.metadata.custom`` is only half the fix while
    the human-facing surface still prints the financial figure with no caveat.
    ``nthlayer-learn retrospective`` is the caller a person actually reads.
    """

    @staticmethod
    def _run_cli(db_path: Path, incident_id: str, specs_dir: str | None) -> None:
        _cmd_retrospective(
            argparse.Namespace(
                db=str(db_path),
                incident_verdict=incident_id,
                specs_dir=specs_dir,
                decision_store=None,
            )
        )

    def test_cli_reports_parse_failures(self, tmp_path: Path, incident_db, capsys):
        specs = _write_specs(tmp_path / "specs", broken=1)
        db, incident_id = incident_db

        self._run_cli(db, incident_id, str(specs))

        assert "Manifest parse failures: 1" in capsys.readouterr().out

    def test_cli_silent_when_no_parse_failures(self, tmp_path: Path, incident_db, capsys):
        specs = _write_specs(tmp_path / "specs")
        db, incident_id = incident_db

        self._run_cli(db, incident_id, str(specs))

        assert "Manifest parse failures" not in capsys.readouterr().out
