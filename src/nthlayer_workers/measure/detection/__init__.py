"""Degradation detection — compares arithmetic against human-declared thresholds (ZFC)."""

from nthlayer_workers.measure.detection.detector import ThresholdDetector
from nthlayer_workers.measure.detection.protocol import Alert, DegradationDetector

__all__ = ["Alert", "DegradationDetector", "ThresholdDetector"]
