"""Tests for OpenSRM manifest loader."""

import pytest

from nthlayer_workers.measure.manifest import load_manifest


@pytest.fixture
def valid_manifest(tmp_path):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        "apiVersion: opensrm/v1\n"
        "kind: ServiceReliabilityManifest\n"
        "metadata:\n"
        "  name: code-reviewer-agent\n"
        "  tier: critical\n"
        "spec:\n"
        "  type: ai-gate\n"
        "  slos:\n"
        "    judgment:\n"
        "      reversal:\n"
        "        rate:\n"
        "          target: 0.05\n"
        "          window: 30d\n"
        "      high_confidence_failure:\n"
        "        target: 0.02\n"
        "        confidence_threshold: 0.9\n"
    )
    return manifest


@pytest.fixture
def no_judgment_manifest(tmp_path):
    manifest = tmp_path / "no_judgment.yaml"
    manifest.write_text(
        "apiVersion: opensrm/v1\n"
        "kind: ServiceReliabilityManifest\n"
        "metadata:\n"
        "  name: simple-agent\n"
        "spec:\n"
        "  type: ai-gate\n"
        "  slos:\n"
        "    latency:\n"
        "      p99: 500ms\n"
    )
    return manifest


def test_load_valid_manifest(valid_manifest):
    slo = load_manifest(valid_manifest)
    assert slo is not None
    assert slo.agent_name == "code-reviewer-agent"
    assert slo.reversal_rate_target == pytest.approx(0.05)
    assert slo.reversal_rate_window_days == 30
    assert slo.high_confidence_failure_target == pytest.approx(0.02)
    assert slo.confidence_threshold == pytest.approx(0.9)


def test_load_manifest_no_judgment_section(no_judgment_manifest):
    slo = load_manifest(no_judgment_manifest)
    assert slo is None


def test_load_manifest_missing_file(tmp_path):
    slo = load_manifest(tmp_path / "nonexistent.yaml")
    assert slo is None


def test_manifest_frozen(valid_manifest):
    slo = load_manifest(valid_manifest)
    assert slo is not None
    with pytest.raises(AttributeError):
        slo.reversal_rate_target = 0.10  # type: ignore[misc]


def test_manifest_feeds_detection_config(valid_manifest):
    """Manifest SLO thresholds can be used to configure detection."""
    slo = load_manifest(valid_manifest)
    assert slo is not None
    # These values should be usable as detection thresholds
    assert 0.0 < slo.reversal_rate_target < 1.0
    assert slo.reversal_rate_window_days > 0
    assert 0.0 < slo.confidence_threshold <= 1.0
