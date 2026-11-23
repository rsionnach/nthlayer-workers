"""Tests for VerdictMetricsCollector — Prometheus metrics from verdict store."""
from __future__ import annotations

import pytest
from nthlayer_common.verdicts import MemoryStore
from nthlayer_common.verdicts import create as verdict_create

from nthlayer_workers.respond.metrics import VerdictMetricsCollector


def _make_verdict(producer_system, subject_type, status="pending"):
    v = verdict_create(
        subject={"type": subject_type, "ref": "test-service", "summary": "test"},
        judgment={"action": "flag", "confidence": 0.9, "reasoning": "test"},
        producer={"system": producer_system},
    )
    return v


def _sample_lines(text):
    return [line for line in text.splitlines() if line and not line.startswith("#")]


def _series_names(text):
    """The metric name of every sample line in a text exposition."""
    return {line.split("{", 1)[0].split(" ", 1)[0] for line in _sample_lines(text)}


def _described_names(text):
    """The metric names that have a HELP or TYPE line."""
    return {
        line.split(" ")[2]
        for line in text.splitlines()
        if line.startswith(("# HELP ", "# TYPE "))
    }


def _samples(text, name):
    """The values of every sample of the series *name*."""
    return [
        float(line.rsplit(" ", 1)[1])
        for line in _sample_lines(text)
        if line.split("{", 1)[0].split(" ", 1)[0] == name
    ]


def test_empty_store_returns_no_gauges():
    """Empty verdict store produces valid but empty metrics."""
    store = MemoryStore()
    collector = VerdictMetricsCollector(store)
    text = collector.collect()
    assert "nthlayer_verdict" not in text
    assert text.startswith("# ")  # Has HELP/TYPE headers


def test_total_count_gauges():
    """Verdicts in store produce nthlayer_verdicts_total gauges."""
    store = MemoryStore()
    for _ in range(3):
        store.put(_make_verdict("nthlayer-measure", "evaluation"))
    for _ in range(2):
        store.put(_make_verdict("nthlayer-correlate", "correlation"))

    collector = VerdictMetricsCollector(store)
    text = collector.collect()
    assert 'nthlayer_verdicts_total{component="measure",verdict_type="evaluation"} 3' in text
    assert 'nthlayer_verdicts_total{component="correlate",verdict_type="correlation"} 2' in text


def test_accuracy_from_resolved_verdicts():
    """Accuracy gauge computed from resolved vs overridden verdicts."""
    store = MemoryStore()
    # 8 confirmed, 2 overridden = 0.8 accuracy
    for i in range(10):
        v = _make_verdict("nthlayer-measure", "evaluation")
        store.put(v)
        if i < 8:
            store.resolve(v.id, "confirmed")
        else:
            store.resolve(v.id, "overridden", override={"by": "human", "reasoning": "wrong"})

    collector = VerdictMetricsCollector(store)
    text = collector.collect()
    # Check accuracy is present (exact value depends on window filtering)
    assert 'nthlayer_verdict_accuracy{component="measure",verdict_type="evaluation"' in text
    # Every series the collector exposes. The overridden / resolved gauge that
    # was named a reversal rate is retired: it was 1 - accuracy, and A1's
    # reversals over all decisions is the only reversal rate [opensrm-l0ki].
    assert _series_names(text) == {"nthlayer_verdicts_total", "nthlayer_verdict_accuracy"}
    assert _described_names(text) == {"nthlayer_verdicts_total", "nthlayer_verdict_accuracy"}
    samples = _samples(text, "nthlayer_verdict_accuracy")
    assert samples and all(value == pytest.approx(0.8) for value in samples)


def test_component_label_strips_prefix():
    """'nthlayer-measure' becomes 'measure' in component label."""
    store = MemoryStore()
    store.put(_make_verdict("nthlayer-respond", "triage"))
    collector = VerdictMetricsCollector(store)
    text = collector.collect()
    assert 'component="respond"' in text


def test_metrics_text_format():
    """Output is valid Prometheus text exposition."""
    store = MemoryStore()
    store.put(_make_verdict("nthlayer-measure", "evaluation"))
    collector = VerdictMetricsCollector(store)
    text = collector.collect()
    # Must have HELP and TYPE lines
    assert "# HELP nthlayer_verdicts_total" in text
    assert "# TYPE nthlayer_verdicts_total gauge" in text
