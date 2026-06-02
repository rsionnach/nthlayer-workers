"""Tests for nthlayer-measure config loading."""


import pytest

from nthlayer_workers.measure.config import VALID_TOP_KEYS, MeasureConfig, load_config


def test_tiering_config_defaults():
    config = MeasureConfig()
    assert config.tiering is None


def test_tiering_config_from_yaml(tmp_path):
    yaml_content = """
evaluator:
  model: claude-sonnet-4-20250514
tiering:
  enabled: true
  default_tier: minimal
  models:
    standard: anthropic/claude-haiku-4-20250414
    deep: anthropic/claude-sonnet-4-20250514
    critical: anthropic/claude-opus-4-20250514
  sampling_rate: 0.10
  promotion_threshold: 0.05
"""
    p = tmp_path / "measure.yaml"
    p.write_text(yaml_content)
    config = load_config(p)
    assert config.tiering is not None
    assert config.tiering.enabled is True
    assert config.tiering.default_tier == "minimal"
    assert config.tiering.sampling_rate == 0.10
    assert config.tiering.promotion_threshold == 0.05
    assert config.tiering.models["standard"] == "anthropic/claude-haiku-4-20250414"


def test_tiering_disabled_by_default(tmp_path):
    p = tmp_path / "measure.yaml"
    p.write_text("evaluator:\n  model: test\n")
    config = load_config(p)
    assert config.tiering is None


def test_unknown_top_level_key_raises(tmp_path):
    p = tmp_path / "measure.yaml"
    p.write_text("evalutator:\n  model: test\n")  # typo: evaluator -> evalutator
    with pytest.raises(ValueError) as excinfo:
        load_config(p)
    msg = str(excinfo.value)
    assert "evalutator" in msg
    assert "evaluator" in msg


def test_legacy_governance_key_raises(tmp_path):
    p = tmp_path / "measure.yaml"
    p.write_text("evaluator:\n  model: test\ngovernance:\n  enabled: true\n")
    with pytest.raises(ValueError) as excinfo:
        load_config(p)
    assert "governance" in str(excinfo.value)


def test_multiple_unknown_keys_listed_sorted(tmp_path):
    p = tmp_path / "measure.yaml"
    p.write_text("zebra:\n  x: 1\nalpha:\n  y: 2\n")
    with pytest.raises(ValueError) as excinfo:
        load_config(p)
    msg = str(excinfo.value)
    assert msg.index("alpha") < msg.index("zebra")


def test_non_mapping_root_raises(tmp_path):
    p = tmp_path / "measure.yaml"
    p.write_text("- item1\n- item2\n")
    with pytest.raises(ValueError, match="must be a mapping"):
        load_config(p)


@pytest.mark.parametrize("body,expected_type", [
    ("hello\n", "str"),
    ("42\n", "int"),
    ("true\n", "bool"),
])
def test_scalar_root_raises(tmp_path, body, expected_type):
    p = tmp_path / "measure.yaml"
    p.write_text(body)
    with pytest.raises(ValueError, match=f"must be a mapping, got {expected_type}"):
        load_config(p)


def test_mixed_type_unknown_keys_do_not_crash_sort(tmp_path):
    p = tmp_path / "measure.yaml"
    # Integer keys are valid YAML; mixed with a valid string key, sorted()
    # would TypeError on str-vs-int comparison without key=str.
    p.write_text("evaluator:\n  model: test\n1: foo\n2: bar\n")
    with pytest.raises(ValueError) as excinfo:
        load_config(p)
    msg = str(excinfo.value)
    assert "Unknown config keys" in msg
    assert "1" in msg and "2" in msg


def test_empty_yaml_returns_defaults(tmp_path):
    p = tmp_path / "measure.yaml"
    p.write_text("")
    config = load_config(p)
    assert config.tiering is None
    assert config.dimensions == ["correctness", "completeness", "safety"]


def test_all_recognised_keys_accepted(tmp_path):
    p = tmp_path / "measure.yaml"
    yaml_content = "\n".join(f"{k}: {{}}" for k in sorted(VALID_TOP_KEYS) if k != "dimensions")
    yaml_content += "\ndimensions: [a, b]\n"
    p.write_text(yaml_content)
    config = load_config(p)
    assert config.dimensions == ["a", "b"]
