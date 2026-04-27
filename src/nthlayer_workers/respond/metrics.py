"""Prometheus metrics from the verdict store.

Exposes verdict accuracy, total counts, and reversal rates as gauges.
Plain text exposition format — no prometheus_client dependency.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from nthlayer_common.verdicts import VerdictFilter


def _component_label(producer_system: str) -> str:
    """Strip 'nthlayer-' prefix for the component label."""
    if producer_system.startswith("nthlayer-"):
        return producer_system[len("nthlayer-"):]
    return producer_system


class VerdictMetricsCollector:
    """Collect verdict metrics from the store and render as Prometheus text."""

    def __init__(self, verdict_store: Any) -> None:
        self._store = verdict_store

    def collect(self) -> str:
        """Query the verdict store and return Prometheus text exposition."""
        # Query all verdicts (no time filter for totals)
        all_verdicts = self._store.query(VerdictFilter(limit=0))

        # Group by (producer_system, subject_type)
        groups: dict[tuple[str, str], list] = {}
        for v in all_verdicts:
            key = (v.producer.system, v.subject.type)
            groups.setdefault(key, []).append(v)

        if not groups:
            # Emit a general comment so text still starts with "# "
            return "# nthlayer_respond metrics: no verdicts recorded\n"

        lines: list[str] = []

        # Emit HELP/TYPE headers only when there is data
        lines.append("# HELP nthlayer_verdicts_total Total number of verdicts by component and type")
        lines.append("# TYPE nthlayer_verdicts_total gauge")

        now = datetime.now(tz=timezone.utc)
        windows = {"7d": timedelta(days=7), "30d": timedelta(days=30)}

        # Collect accuracy/reversal lines separately so we can emit headers only if needed
        accuracy_lines: list[str] = []

        for (producer, subject_type), verdicts in sorted(groups.items()):
            component = _component_label(producer)

            # Total count (no window)
            lines.append(
                f'nthlayer_verdicts_total{{component="{component}",'
                f'verdict_type="{subject_type}"}} {len(verdicts)}'
            )

            # Accuracy and reversal rate per window
            for window_label, delta in windows.items():
                cutoff = now - delta
                windowed = [
                    v for v in verdicts
                    if v.timestamp and v.timestamp >= cutoff
                ]
                if not windowed:
                    continue

                resolved = [
                    v for v in windowed
                    if v.outcome and v.outcome.status in ("confirmed", "overridden", "partial")
                ]
                if not resolved:
                    continue

                overridden = sum(
                    1 for v in resolved if v.outcome.status == "overridden"
                )
                total_resolved = len(resolved)
                accuracy = 1.0 - (overridden / total_resolved)
                reversal = overridden / total_resolved

                accuracy_lines.append(
                    f'nthlayer_verdict_accuracy{{component="{component}",'
                    f'verdict_type="{subject_type}",window="{window_label}"}} {accuracy:.4f}'
                )
                accuracy_lines.append(
                    f'nthlayer_verdict_reversal_rate{{component="{component}",'
                    f'verdict_type="{subject_type}",window="{window_label}"}} {reversal:.4f}'
                )

        if accuracy_lines:
            lines.append("# HELP nthlayer_verdict_accuracy Verdict accuracy (1 - reversal rate) by component")
            lines.append("# TYPE nthlayer_verdict_accuracy gauge")
            lines.append("# HELP nthlayer_verdict_reversal_rate Verdict reversal rate (overridden / total resolved) by component")
            lines.append("# TYPE nthlayer_verdict_reversal_rate gauge")
            lines.extend(accuracy_lines)

        lines.append("")  # trailing newline
        return "\n".join(lines)
