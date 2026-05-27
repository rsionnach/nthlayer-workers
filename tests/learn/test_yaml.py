"""Unit tests for nthlayer_workers.learn._yaml (jmy.6)."""
from __future__ import annotations

import pytest
from ruamel.yaml import YAML


@pytest.fixture
def parsed_manifest():
    """Sample manifest parsed via ruamel.yaml (preserves comments)."""
    yaml = YAML(typ="rt")  # round-trip mode
    text = (
        "metadata:\n"
        "  name: fraud-detect\n"
        "spec:\n"
        "  slos:\n"
        "    judgment:\n"
        "      target: 95.0  # current SLO target\n"
        "      window: 30d\n"
    )
    return yaml.load(text)


class TestResolvePath:
    """resolve_path traverses dotted paths through CommentedMap."""

    def test_resolve_path_happy(self, parsed_manifest):
        from nthlayer_workers.learn._yaml import resolve_path

        assert resolve_path(parsed_manifest, "spec.slos.judgment.target") == 95.0

    def test_resolve_path_missing_leaf(self, parsed_manifest):
        from nthlayer_workers.learn._yaml import resolve_path, PATH_MISSING

        result = resolve_path(parsed_manifest, "spec.slos.judgment.nonexistent")
        assert result is PATH_MISSING

    def test_resolve_path_missing_intermediate(self, parsed_manifest):
        from nthlayer_workers.learn._yaml import resolve_path, PATH_MISSING

        result = resolve_path(parsed_manifest, "spec.deployment.gates.judgment")
        assert result is PATH_MISSING

    def test_resolve_path_empty_path_returns_root(self, parsed_manifest):
        from nthlayer_workers.learn._yaml import resolve_path

        result = resolve_path(parsed_manifest, "")
        assert "metadata" in result
        assert "spec" in result
