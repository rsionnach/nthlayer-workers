"""Tests for tier classification."""
import pytest

from nthlayer_workers.measure.config import TieringConfig
from nthlayer_workers.measure.tiering.classifier import TierClassifier
from nthlayer_workers.measure.types import AgentOutput

VALID_TIERS = {"minimal", "standard", "deep", "critical"}


def _make_output(agent="test-agent"):
    return AgentOutput(
        agent_name=agent, task_id="t1",
        output_content="hello", output_type="api",
    )


@pytest.fixture
def config():
    return TieringConfig(enabled=True, default_tier="standard")


@pytest.fixture
def classifier(config):
    return TierClassifier(config, manifests={})


def test_caller_override_wins(classifier):
    result = classifier.classify(_make_output(), metadata={"risk_tier": "critical"})
    assert result == "critical"


def test_manifest_default(config):
    manifests = {"test-agent": {"tier": "minimal"}}
    c = TierClassifier(config, manifests=manifests)
    result = c.classify(_make_output())
    assert result == "minimal"


def test_config_default(classifier):
    result = classifier.classify(_make_output())
    assert result == "standard"


def test_fallback_when_no_config():
    c = TierClassifier(TieringConfig(enabled=True, default_tier="deep"), manifests={})
    result = c.classify(_make_output())
    assert result == "deep"


def test_invalid_tier_falls_back(classifier):
    result = classifier.classify(_make_output(), metadata={"risk_tier": "bogus"})
    assert result in VALID_TIERS


def test_caller_overrides_manifest(config):
    manifests = {"test-agent": {"tier": "minimal"}}
    c = TierClassifier(config, manifests=manifests)
    result = c.classify(_make_output(), metadata={"risk_tier": "critical"})
    assert result == "critical"


def test_should_sample_returns_bool(classifier):
    result = classifier.should_sample("minimal", "test-agent")
    assert isinstance(result, bool)


def test_should_sample_non_minimal_always_false(classifier):
    assert classifier.should_sample("standard", "test-agent") is False
    assert classifier.should_sample("deep", "test-agent") is False
    assert classifier.should_sample("critical", "test-agent") is False


def test_should_sample_respects_rate():
    config = TieringConfig(enabled=True, sampling_rate=1.0)  # 100% sampling
    c = TierClassifier(config, manifests={})
    assert c.should_sample("minimal", "test-agent") is True


def test_should_sample_manifest_rate_override():
    config = TieringConfig(enabled=True, sampling_rate=0.0)  # 0% global
    manifests = {"test-agent": {"sampling_rate": 1.0}}  # 100% for this agent
    c = TierClassifier(config, manifests=manifests)
    assert c.should_sample("minimal", "test-agent") is True
