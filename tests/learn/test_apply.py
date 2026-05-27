"""Unit tests for nthlayer_workers.learn._apply (jmy.6)."""
from __future__ import annotations

import pytest
from pathlib import Path


class TestResolveManifestPath:
    """resolve_manifest_path: filename-convention + walk fallback."""

    def test_filename_convention(self, tmp_path):
        from nthlayer_workers.learn._apply import resolve_manifest_path

        (tmp_path / "fraud-detect.yaml").write_text(
            "metadata:\n  name: fraud-detect\nspec:\n  slos: {}\n"
        )

        result = resolve_manifest_path("fraud-detect", tmp_path)
        assert result == tmp_path / "fraud-detect.yaml"

    def test_filename_convention_yml_extension(self, tmp_path):
        from nthlayer_workers.learn._apply import resolve_manifest_path

        (tmp_path / "fraud-detect.yml").write_text(
            "metadata:\n  name: fraud-detect\nspec:\n  slos: {}\n"
        )

        result = resolve_manifest_path("fraud-detect", tmp_path)
        assert result == tmp_path / "fraud-detect.yml"

    def test_walk_fallback_finds_by_metadata_name(self, tmp_path):
        from nthlayer_workers.learn._apply import resolve_manifest_path

        # File doesn't match service name; metadata.name does
        nested = tmp_path / "payments" / "billing-srv.yaml"
        nested.parent.mkdir(parents=True)
        nested.write_text(
            "metadata:\n  name: fraud-detect\nspec:\n  slos: {}\n"
        )

        result = resolve_manifest_path("fraud-detect", tmp_path)
        assert result == nested

    def test_hidden_dirs_excluded(self, tmp_path):
        from nthlayer_workers.learn._apply import resolve_manifest_path

        # Hidden dir; should be excluded from walk
        hidden = tmp_path / ".cache" / "fraud-detect.yaml"
        hidden.parent.mkdir()
        hidden.write_text(
            "metadata:\n  name: fraud-detect\nspec:\n  slos: {}\n"
        )

        result = resolve_manifest_path("fraud-detect", tmp_path)
        assert result is None  # not found (hidden excluded)

    def test_not_found_returns_none(self, tmp_path):
        from nthlayer_workers.learn._apply import resolve_manifest_path

        result = resolve_manifest_path("nonexistent-service", tmp_path)
        assert result is None
