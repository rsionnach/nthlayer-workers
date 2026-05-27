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


class TestApplyAtPath:
    """apply_at_path writes in-place; comments survive round-trip."""

    def test_apply_at_existing_leaf(self):
        from nthlayer_workers.learn._yaml import apply_at_path, get_yaml_round_trip
        from io import StringIO

        yaml = get_yaml_round_trip()
        text = (
            "spec:\n"
            "  slos:\n"
            "    judgment:\n"
            "      target: 95.0  # current SLO target\n"
        )
        doc = yaml.load(text)

        apply_at_path(doc, "spec.slos.judgment.target", 98.5)

        buf = StringIO()
        yaml.dump(doc, buf)
        output = buf.getvalue()

        # New value present
        assert "target: 98.5" in output
        # Old value gone
        assert "target: 95.0" not in output
        # Comment preserved
        assert "# current SLO target" in output

    def test_apply_at_missing_intermediate_creates(self):
        from nthlayer_workers.learn._yaml import apply_at_path, get_yaml_round_trip
        from io import StringIO

        yaml = get_yaml_round_trip()
        text = "spec:\n  slos:\n    reversal_rate:\n      target: 98.5\n"
        doc = yaml.load(text)

        # Path doesn't exist; apply creates intermediates
        apply_at_path(doc, "spec.deployment.gates.judgment", {
            "enabled": True,
            "block_on": ["reversal_rate"],
        })

        buf = StringIO()
        yaml.dump(doc, buf)
        output = buf.getvalue()

        assert "deployment:" in output
        assert "gates:" in output
        assert "judgment:" in output
        assert "enabled: true" in output

    def test_apply_at_path_preserves_sibling_comments(self):
        from nthlayer_workers.learn._yaml import apply_at_path, get_yaml_round_trip
        from io import StringIO

        yaml = get_yaml_round_trip()
        text = (
            "spec:\n"
            "  slos:\n"
            "    # SLO for the judgment pipeline\n"
            "    judgment:\n"
            "      target: 95.0\n"
            "      window: 30d  # rolling window\n"
        )
        doc = yaml.load(text)

        apply_at_path(doc, "spec.slos.judgment.target", 98.5)

        buf = StringIO()
        yaml.dump(doc, buf)
        output = buf.getvalue()

        assert "# SLO for the judgment pipeline" in output
        assert "# rolling window" in output


class TestNormalizeScalar:
    """normalize_scalar enables int/float/numeric-string equivalence."""

    def test_int_float_str_numeric_equivalence(self):
        from nthlayer_workers.learn._yaml import normalize_scalar

        n_int = normalize_scalar(98)
        n_float = normalize_scalar(98.0)
        n_str_int = normalize_scalar("98")
        n_str_float = normalize_scalar("98.0")

        assert n_int == n_float == n_str_int == n_str_float

    def test_different_numbers_not_equivalent(self):
        from nthlayer_workers.learn._yaml import normalize_scalar

        assert normalize_scalar(98) != normalize_scalar(99)
        assert normalize_scalar(98.5) != normalize_scalar(98.6)

    def test_non_numeric_string_returns_as_is(self):
        from nthlayer_workers.learn._yaml import normalize_scalar

        assert normalize_scalar("hello") == "hello"
        assert normalize_scalar("30d") == "30d"
        assert normalize_scalar(30) != normalize_scalar("30d")

    def test_bool_not_treated_as_numeric(self):
        """bool subclasses int in Python; we explicitly do NOT coerce."""
        from nthlayer_workers.learn._yaml import normalize_scalar

        assert normalize_scalar(True) != normalize_scalar(1)
        assert normalize_scalar(False) != normalize_scalar(0)

    def test_non_scalar_passes_through(self):
        from nthlayer_workers.learn._yaml import normalize_scalar

        assert normalize_scalar({"a": 1}) == {"a": 1}
        assert normalize_scalar([1, 2]) == [1, 2]
