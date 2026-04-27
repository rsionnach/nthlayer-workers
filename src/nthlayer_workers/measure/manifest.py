"""OpenSRM manifest loader — reads judgment SLO thresholds from manifest YAML.

Pure YAML parsing + field extraction (ZFC: transport, not judgment).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class JudgmentSLO:
    """Judgment SLO targets extracted from an OpenSRM manifest."""

    agent_name: str
    reversal_rate_target: float
    reversal_rate_window_days: int
    high_confidence_failure_target: float
    confidence_threshold: float
    quality_threshold: float | None = None


def _parse_window(window_str: str) -> int:
    """Parse a window string like '30d' into an integer number of days.

    Supported formats: '30d', '30', or bare integer 30.
    """
    s = str(window_str).strip().lower()
    if s.endswith("d"):
        s = s[:-1]
    if not s:
        raise ValueError(f"Invalid window value: {window_str!r}")
    try:
        return int(s)
    except ValueError:
        raise ValueError(
            f"Invalid window value: {window_str!r}. Expected '<int>d' or '<int>' (e.g. '30d')"
        ) from None


def load_manifest(path: Path) -> JudgmentSLO | None:
    """Load judgment SLO from an OpenSRM manifest.

    Returns None if the manifest has no judgment section.
    """
    if not path.exists():
        return None

    raw = yaml.safe_load(path.read_text())
    if raw is None:
        return None

    metadata = raw.get("metadata", {})
    agent_name = metadata.get("name", "")

    spec = raw.get("spec", {})
    slos = spec.get("slos", {})
    judgment = slos.get("judgment")

    if judgment is None:
        return None

    reversal = judgment.get("reversal", {}).get("rate", {})
    hcf = judgment.get("high_confidence_failure", {})
    raw_qt = judgment.get("quality_threshold")

    return JudgmentSLO(
        agent_name=agent_name,
        reversal_rate_target=float(reversal.get("target", 0.05)),
        reversal_rate_window_days=_parse_window(reversal.get("window", "30d")),
        high_confidence_failure_target=float(hcf.get("target", 0.02)),
        confidence_threshold=float(hcf.get("confidence_threshold", 0.9)),
        quality_threshold=float(raw_qt) if raw_qt is not None else None,
    )
