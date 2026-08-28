"""Manifest parse failures are logged and counted (opensrm-oh27).

``_load_manifests_from_specs`` used to swallow ``ManifestLoadError`` with a
bare ``continue`` — no log, no counter, no re-raise. Because the retrospective
computes financial impact per service, that did not produce a questionable
number; it produced a *confident* number computed over a subset, with the
dropped service indistinguishable from one that genuinely had no impact.

The loop's very next branch already logs when it skips a duplicate, so the
convention was established and only the parse-failure branch ignored it.
These tests pin both halves of the fix: the warning log carrying file path
and error, and the failure count reaching the caller on the result object.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import structlog
from nthlayer_common.verdicts.core import create
from nthlayer_common.verdicts.store import MemoryStore

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


def _write_specs(tmp_path: Path, *, broken: int = 0, good: bool = True) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    if good:
        (tmp_path / "svc-good.yaml").write_text(GOOD_MANIFEST)
    for i in range(broken):
        (tmp_path / f"svc-broken-{i}.yaml").write_text(
            BROKEN_MANIFEST.replace("svc-broken", f"svc-broken-{i}")
        )
    return tmp_path


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

    @staticmethod
    def _incident():
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

    def test_parse_failure_count_present_in_retro_custom(self, tmp_path: Path):
        _write_specs(tmp_path, broken=1)
        store = MemoryStore()
        incident = self._incident()
        store.put(incident)

        retro = build_retrospective(incident.id, store, specs_dir=str(tmp_path))

        assert retro.metadata.custom["manifest_parse_failures"] == 1

    def test_parse_failure_count_zero_when_no_specs_dir(self):
        store = MemoryStore()
        incident = self._incident()
        store.put(incident)

        retro = build_retrospective(incident.id, store)

        assert retro.metadata.custom["manifest_parse_failures"] == 0


class TestCliSurfacesParseFailures:
    """R5 pass 1: the count reaching ``retro.metadata.custom`` is only half
    the fix while the human-facing surface still prints the financial figure
    with no caveat. ``nthlayer-learn retrospective`` is the caller a person
    actually reads.
    """

    @staticmethod
    def _run_cli(db_path: Path, incident_id: str, specs_dir: str | None):
        import argparse

        from nthlayer_workers.learn.cli import _cmd_retrospective

        _cmd_retrospective(
            argparse.Namespace(
                db=str(db_path),
                incident_verdict=incident_id,
                specs_dir=specs_dir,
                decision_store=None,
            )
        )

    def test_cli_reports_parse_failures(self, tmp_path: Path, capsys):
        from nthlayer_common.verdicts.sqlite_store import SQLiteVerdictStore

        specs = _write_specs(tmp_path / "specs", broken=1)
        db = tmp_path / "verdicts.db"
        store = SQLiteVerdictStore(str(db))
        incident = TestRetrospectiveSurfacesParseFailures._incident()
        store.put(incident)
        store.close()

        self._run_cli(db, incident.id, str(specs))

        out = capsys.readouterr().out
        assert "Manifest parse failures: 1" in out

    def test_cli_silent_when_no_parse_failures(self, tmp_path: Path, capsys):
        from nthlayer_common.verdicts.sqlite_store import SQLiteVerdictStore

        specs = _write_specs(tmp_path / "specs")
        db = tmp_path / "verdicts.db"
        store = SQLiteVerdictStore(str(db))
        incident = TestRetrospectiveSurfacesParseFailures._incident()
        store.put(incident)
        store.close()

        self._run_cli(db, incident.id, str(specs))

        assert "Manifest parse failures" not in capsys.readouterr().out
