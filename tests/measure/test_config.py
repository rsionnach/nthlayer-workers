"""Tests for nthlayer-measure config loading."""



from nthlayer_workers.measure.config import MeasureConfig, load_config


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
