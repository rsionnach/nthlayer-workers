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


class TestClassifyOutcome:
    """Two-table state machine per jmy.6 design § 5 + § 7."""

    @pytest.fixture
    def rec_with_current(self):
        """Recommendation modifying existing state (e.g. tighten_slo)."""
        from nthlayer_workers.learn.recommendations import Recommendation
        return Recommendation(
            id="rec-deadbeef0123",
            service="fraud-detect",
            type="tighten_slo",
            rationale="test",
            field="spec.slos.judgment.target",
            current_value=95.0,
            proposed_value=98.5,
        )

    @pytest.fixture
    def rec_without_current(self):
        """Recommendation adding new state (e.g. add_deploy_gate)."""
        from nthlayer_workers.learn.recommendations import Recommendation
        return Recommendation(
            id="rec-deadbeef0124",
            service="fraud-detect",
            type="add_deploy_gate",
            rationale="test",
            field="spec.deployment.gates.judgment",
            current_value=None,
            proposed_value={"enabled": True, "block_on": ["reversal_rate"]},
        )

    # 4 cells for "modifying existing" (current_value present)

    def test_with_current_path_missing_target_path_missing(self, rec_with_current):
        from nthlayer_workers.learn._yaml import classify_outcome, PATH_MISSING
        from nthlayer_workers.learn.recommendations import OutcomeKind

        result = classify_outcome(PATH_MISSING, rec_with_current)
        assert result == OutcomeKind.TARGET_PATH_MISSING

    def test_with_current_path_eq_proposed_already_applied(self, rec_with_current):
        from nthlayer_workers.learn._yaml import classify_outcome
        from nthlayer_workers.learn.recommendations import OutcomeKind

        result = classify_outcome(98.5, rec_with_current)
        assert result == OutcomeKind.ALREADY_APPLIED

    def test_with_current_path_eq_current_apply_clean(self, rec_with_current):
        from nthlayer_workers.learn._yaml import classify_outcome
        from nthlayer_workers.learn.recommendations import OutcomeKind

        result = classify_outcome(95.0, rec_with_current)
        assert result == OutcomeKind.APPLY_CLEAN

    def test_with_current_path_eq_other_drift_detected(self, rec_with_current):
        from nthlayer_workers.learn._yaml import classify_outcome
        from nthlayer_workers.learn.recommendations import OutcomeKind

        result = classify_outcome(97.0, rec_with_current)
        assert result == OutcomeKind.DRIFT_DETECTED

    # 3 cells for "adding new" (current_value None)

    def test_without_current_path_missing_apply_clean(self, rec_without_current):
        from nthlayer_workers.learn._yaml import classify_outcome, PATH_MISSING
        from nthlayer_workers.learn.recommendations import OutcomeKind

        result = classify_outcome(PATH_MISSING, rec_without_current)
        assert result == OutcomeKind.APPLY_CLEAN

    def test_without_current_path_eq_proposed_already_applied(self, rec_without_current):
        from nthlayer_workers.learn._yaml import classify_outcome
        from nthlayer_workers.learn.recommendations import OutcomeKind

        result = classify_outcome(
            {"enabled": True, "block_on": ["reversal_rate"]},
            rec_without_current,
        )
        assert result == OutcomeKind.ALREADY_APPLIED

    def test_without_current_path_other_drift_detected(self, rec_without_current):
        from nthlayer_workers.learn._yaml import classify_outcome
        from nthlayer_workers.learn.recommendations import OutcomeKind

        result = classify_outcome(
            {"enabled": False, "block_on": []},
            rec_without_current,
        )
        assert result == OutcomeKind.DRIFT_DETECTED

    # Normalisation interactions

    def test_normalisation_int_vs_float_drift(self, rec_with_current):
        """Manifest has 98 (int); proposed is 98.5 (float). Different values → drift."""
        from nthlayer_workers.learn._yaml import classify_outcome
        from nthlayer_workers.learn.recommendations import OutcomeKind

        result = classify_outcome(98, rec_with_current)
        assert result == OutcomeKind.DRIFT_DETECTED

    def test_normalisation_numeric_string_eq_proposed(self, rec_with_current):
        """Manifest has '98.5' (string); proposed is 98.5 (float). Equivalent → already_applied."""
        from nthlayer_workers.learn._yaml import classify_outcome
        from nthlayer_workers.learn.recommendations import OutcomeKind

        result = classify_outcome("98.5", rec_with_current)
        assert result == OutcomeKind.ALREADY_APPLIED


# ---------------------------------------------------------------------------
# jmy.21: list-append sigil ``[+]`` (apply_at_path + classify_outcome)
# ---------------------------------------------------------------------------


class TestSigilAppend:
    """``spec.dependencies[+]`` semantics for add_dependency recs (jmy.21)."""

    def test_apply_at_path_appends_to_existing_list(self):
        from io import StringIO

        from nthlayer_workers.learn._yaml import apply_at_path, get_yaml_round_trip

        yaml = get_yaml_round_trip()
        text = (
            "spec:\n"
            "  dependencies:\n"
            "    - name: a\n"
            "      type: api\n"
        )
        doc = yaml.load(text)

        apply_at_path(doc, "spec.dependencies[+]", {"name": "b", "type": "x"})

        deps = doc["spec"]["dependencies"]
        assert len(deps) == 2
        names = [d["name"] for d in deps]
        assert names == ["a", "b"]

        # Round-trip cleanly
        buf = StringIO()
        yaml.dump(doc, buf)
        output = buf.getvalue()
        assert "name: a" in output
        assert "name: b" in output

    def test_apply_at_path_creates_list_at_missing_leaf(self):
        from nthlayer_workers.learn._yaml import apply_at_path, get_yaml_round_trip

        yaml = get_yaml_round_trip()
        text = "spec:\n  slos: {}\n"
        doc = yaml.load(text)

        apply_at_path(doc, "spec.dependencies[+]", {"name": "a"})

        deps = doc["spec"]["dependencies"]
        assert isinstance(deps, list)
        assert len(deps) == 1
        assert deps[0]["name"] == "a"

    def test_apply_at_path_creates_intermediates_for_sigil(self):
        from nthlayer_workers.learn._yaml import apply_at_path, get_yaml_round_trip

        yaml = get_yaml_round_trip()
        # Empty doc — full chain must be materialised.
        doc = yaml.load("{}\n")

        apply_at_path(doc, "a.b.c[+]", {"name": "x"})

        assert doc["a"]["b"]["c"] == [{"name": "x"}]

    def test_apply_at_path_raises_when_leaf_is_non_list(self):
        from nthlayer_workers.learn._yaml import apply_at_path, get_yaml_round_trip

        yaml = get_yaml_round_trip()
        text = "spec:\n  dependencies: 5\n"
        doc = yaml.load(text)

        with pytest.raises(TypeError, match="dependencies"):
            apply_at_path(doc, "spec.dependencies[+]", {"name": "a"})

    def test_classify_outcome_append_path_missing_returns_apply_clean(self):
        from nthlayer_workers.learn._yaml import PATH_MISSING, classify_outcome
        from nthlayer_workers.learn.recommendations import OutcomeKind, Recommendation

        rec = Recommendation(
            id="rec-deadbeef0125",
            service="svc-A",
            type="add_dependency",
            rationale="test",
            field="spec.dependencies[+]",
            current_value=None,
            proposed_value={"name": "svc-Y", "type": "unknown"},
        )
        assert classify_outcome(PATH_MISSING, rec) == OutcomeKind.APPLY_CLEAN

    def test_classify_outcome_append_already_present_by_name(self):
        from nthlayer_workers.learn._yaml import classify_outcome
        from nthlayer_workers.learn.recommendations import OutcomeKind, Recommendation

        rec = Recommendation(
            id="rec-deadbeef0126",
            service="svc-A",
            type="add_dependency",
            rationale="test",
            field="spec.dependencies[+]",
            current_value=None,
            proposed_value={"name": "a", "type": "x"},
        )
        manifest_list = [{"name": "a", "type": "api"}, {"name": "b"}]
        assert classify_outcome(manifest_list, rec) == OutcomeKind.ALREADY_APPLIED

    def test_classify_outcome_append_absent_by_name(self):
        from nthlayer_workers.learn._yaml import classify_outcome
        from nthlayer_workers.learn.recommendations import OutcomeKind, Recommendation

        rec = Recommendation(
            id="rec-deadbeef0127",
            service="svc-A",
            type="add_dependency",
            rationale="test",
            field="spec.dependencies[+]",
            current_value=None,
            proposed_value={"name": "b", "type": "x"},
        )
        manifest_list = [{"name": "a"}]
        assert classify_outcome(manifest_list, rec) == OutcomeKind.APPLY_CLEAN

    def test_classify_outcome_append_non_list_returns_drift(self):
        from nthlayer_workers.learn._yaml import classify_outcome
        from nthlayer_workers.learn.recommendations import OutcomeKind, Recommendation

        rec = Recommendation(
            id="rec-deadbeef0128",
            service="svc-A",
            type="add_dependency",
            rationale="test",
            field="spec.dependencies[+]",
            current_value=None,
            proposed_value={"name": "x"},
        )
        assert classify_outcome({"some": "dict"}, rec) == OutcomeKind.DRIFT_DETECTED

    def test_classify_outcome_append_scalar_proposed_uses_deep_eq(self):
        """Non-dict proposed (no ``name`` key) falls back to deep ``==``."""
        from nthlayer_workers.learn._yaml import classify_outcome
        from nthlayer_workers.learn.recommendations import OutcomeKind, Recommendation

        rec = Recommendation(
            id="rec-deadbeef0129",
            service="svc-A",
            type="add_dependency",
            rationale="test",
            field="spec.dependencies[+]",
            current_value=None,
            proposed_value="a",
        )
        assert classify_outcome(["a", "b"], rec) == OutcomeKind.ALREADY_APPLIED
        assert classify_outcome(["b", "c"], rec) == OutcomeKind.APPLY_CLEAN

    # ---- jmy.21 P3 R5 coverage gaps ----

    def test_apply_at_path_raises_on_none_doc(self):
        """jmy.21 P3 R5: doc=None must raise structural TypeError, not
        the cryptic 'NoneType is not iterable' from key membership."""
        from nthlayer_workers.learn._yaml import apply_at_path
        with pytest.raises(TypeError, match="non-mapping document"):
            apply_at_path(None, "spec.dependencies[+]", {"name": "x"})

    def test_apply_at_path_raises_on_bare_sigil(self):
        """jmy.21 P3 R5: `[+]` with no base path raises ValueError."""
        from nthlayer_workers.learn._yaml import apply_at_path
        with pytest.raises(ValueError, match="non-empty base path"):
            apply_at_path({}, "[+]", {"name": "x"})

    def test_apply_at_path_sigil_raises_on_non_mapping_intermediate(self):
        """jmy.21 P3 R5: sigil descent through a scalar intermediate
        raises the same structural TypeError as the set-path branch."""
        from nthlayer_workers.learn._yaml import apply_at_path
        doc = {"spec": 5}
        with pytest.raises(TypeError, match="non-mapping"):
            apply_at_path(doc, "spec.deps[+]", {"name": "x"})

    def test_classify_outcome_append_mixed_list_scalar_and_dict(self):
        """jmy.21 P3 R5: a list containing both scalars and dicts must
        still match the proposed dict-with-name against existing dicts
        (scalar items skipped by the type guard)."""
        from nthlayer_workers.learn._yaml import classify_outcome
        from nthlayer_workers.learn.recommendations import OutcomeKind, Recommendation

        rec = Recommendation(
            id="rec-deadbeef0130",
            service="svc-A",
            type="add_dependency",
            rationale="test",
            field="spec.dependencies[+]",
            current_value=None,
            proposed_value={"name": "svc-Y"},
        )
        # Scalar "a-string" is silently skipped; dict-with-name matches.
        assert classify_outcome(
            ["a-string", {"name": "svc-Y", "type": "api"}], rec,
        ) == OutcomeKind.ALREADY_APPLIED
        assert classify_outcome(
            ["a-string", {"name": "svc-Z"}], rec,
        ) == OutcomeKind.APPLY_CLEAN
