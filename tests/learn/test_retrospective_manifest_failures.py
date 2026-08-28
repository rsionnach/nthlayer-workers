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


class TestCountCountsOnlyManifests:
    """R5 pass 3. ``load_manifest`` raises ``ManifestLoadError`` for *any*
    YAML it cannot parse as a manifest, including files that never claimed to
    be one — a ``kustomization.yaml``, a Prometheus rules file. Counting those
    would fire the CLI's "computed over a subset" caveat on every run over a
    mixed directory, and a caveat that always fires stops being read.

    So the count is of files that *declare themselves* manifests (v1/v2
    apiVersion+kind, or the legacy ``service:`` shape) and failed anyway,
    plus YAML too malformed to make that determination.
    """

    def test_foreign_yaml_is_not_counted(self, tmp_path: Path):
        specs = _write_specs(tmp_path / "specs")
        (specs / "kustomization.yaml").write_text(
            "resources:\n  - deployment.yaml\n"
        )

        loaded_specs = _load_manifests_from_specs(str(specs))

        assert loaded_specs.parse_failures == 0
        assert set(loaded_specs.manifests) == {"svc-good"}

    def test_foreign_yaml_does_not_log_a_parse_failure(self, tmp_path: Path):
        specs = _write_specs(tmp_path / "specs")
        (specs / "kustomization.yaml").write_text(
            "resources:\n  - deployment.yaml\n"
        )

        with structlog.testing.capture_logs() as logs:
            _load_manifests_from_specs(str(specs))

        assert [e for e in logs if e["event"] == "manifest_parse_failed"] == []

    def test_declared_manifest_that_fails_is_still_counted(self, tmp_path: Path):
        """The bead's case: a file that says `kind: ServiceManifest` and then
        does not parse is exactly what the count exists for.
        """
        specs = _write_specs(tmp_path / "specs", broken=1)

        assert _load_manifests_from_specs(str(specs)).parse_failures == 1

    def test_unparseable_yaml_is_counted(self, tmp_path: Path):
        """Too malformed to tell whether it claimed to be a manifest. Counting
        it is the conservative read — a YAML syntax error inside a specs dir is
        a deployment error either way.
        """
        specs = _write_specs(tmp_path / "specs")
        (specs / "torn.yaml").write_text(": : : [unclosed\n")

        assert _load_manifests_from_specs(str(specs)).parse_failures == 1


class TestSpecsDirShapes:
    """R5 pass 3: directory-level edge cases."""

    def test_yml_extension_manifests_are_loaded(self, tmp_path: Path):
        """A `.yml` manifest used to be invisible to the glob — dropped from
        the financial figure with `parse_failures == 0`, which is the same
        silent-subset failure this bead fixes, reached by file extension
        instead of by parse error. `observe/slo/spec_loader.py` has always
        accepted both suffixes.
        """
        specs = _write_specs(tmp_path / "specs")
        (specs / "svc-yml.yml").write_text(
            GOOD_MANIFEST.replace("svc-good", "svc-yml")
        )

        loaded_specs = _load_manifests_from_specs(str(specs))

        assert set(loaded_specs.manifests) == {"svc-good", "svc-yml"}
        assert loaded_specs.parse_failures == 0

    def test_empty_specs_dir(self, tmp_path: Path):
        empty = tmp_path / "empty"
        empty.mkdir()

        loaded_specs = _load_manifests_from_specs(str(empty))

        assert loaded_specs.manifests == {}
        assert loaded_specs.parse_failures == 0

    def test_broken_overlay_counts_while_base_still_loads(self, tmp_path: Path):
        """Per-file semantics, pinned: a service whose base manifest parsed is
        still present even though one of its files failed. The count is a
        coverage-doubt signal, not a miss-count.
        """
        specs = _write_specs(tmp_path / "specs")
        (specs / "svc-good-overlay.yaml").write_text(BROKEN_MANIFEST)

        loaded_specs = _load_manifests_from_specs(str(specs))

        assert loaded_specs.parse_failures == 1
        assert "svc-good" in loaded_specs.manifests

    def test_every_manifest_broken_yields_no_financial_figure(
        self, tmp_path: Path, incident_db, capsys
    ):
        """The worst case, end to end: nothing parsed, so there is no financial
        line at all — and the caveat is the only thing telling the reader why.
        """
        specs = tmp_path / "specs"
        specs.mkdir()
        (specs / "svc-broken.yaml").write_text(BROKEN_MANIFEST)
        db, incident_id = incident_db

        TestCliSurfacesParseFailures._run_cli(db, incident_id, str(specs))

        out = capsys.readouterr().out
        assert "Financial impact" not in out
        assert "Manifest parse failures: 1" in out
