"""Judgment SLO targets for the calibrate report, read from a manifest.

The manifest is read by nthlayer-common's loader, so this path accepts what
every other NthLayer component accepts (opensrm.nthlayer.io/v2, srm/v1, the
legacy format) and rejects what they reject: the published opensrm/v1 schema
is migrate-only, and the loader says how to migrate it [opensrm-n4v1].

Pure field extraction (ZFC: transport, not judgment).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from nthlayer_common.manifest import ManifestLoadError
from nthlayer_common.manifest import load_manifest as load_reliability_manifest
from nthlayer_common.manifest.target_validation import (
    JUDGMENT_TARGET_FIELDS,
    judgment_target_ratio,
)

_REVERSAL_RATE = "reversal_rate"
# Far finer than any declared ratio, far coarser than the float noise.
_RATIO_DECIMALS = 12


@dataclass(frozen=True)
class JudgmentSLO:
    """The judgment targets JudgmentSLOChecker compares a report against."""

    agent_name: str
    # The most reversals allowed, as a ratio of evaluations (0.05 = 5%): what
    # a v2 manifest declares as target.maximum_reversal_rate.
    reversal_rate_target: float
    # The score below which an evaluation counts as rejected. No manifest
    # format declares it, so load_manifest never sets it; a caller that has
    # one from elsewhere may pass it. Without it the report's false-accept
    # rate, precision and recall are None.
    quality_threshold: float | None = None


def load_manifest(path: Path) -> JudgmentSLO | None:
    """The reversal-rate judgment SLO a manifest declares, or None if it
    declares none.

    Raises FileNotFoundError for a missing file and ManifestLoadError for one
    nthlayer-common's loader rejects, or one that declares more than one
    reversal-rate SLO (there would be no telling which one the report is for).
    """
    manifest = load_reliability_manifest(path)

    reversal = [s for s in manifest.slos if s.judgment_type == _REVERSAL_RATE]
    if not reversal:
        return None
    if len(reversal) > 1:
        names = ", ".join(sorted(s.name for s in reversal))
        raise ManifestLoadError(
            f"{path}: {len(reversal)} reversal_rate judgment SLOs ({names}); "
            "the calibrate report compares against exactly one"
        )

    # SLODefinition.target is the 0-100 SLI floor; the report compares a
    # reversal ratio against the declared ceiling, so convert it back.
    try:
        ceiling = judgment_target_ratio(
            JUDGMENT_TARGET_FIELDS[_REVERSAL_RATE], reversal[0].target
        )
    except ValueError as exc:
        raise ManifestLoadError(f"{path}: reversal_rate SLO '{reversal[0].name}': {exc}") from exc
    # The round trip through the floor leaves float noise of about 1e-15
    # (0.05 comes back as 0.050000000000000044). The report tests
    # ``reversal_rate <= target``, so take the noise off: a rate exactly at
    # the declared ceiling must read as compliant.
    return JudgmentSLO(
        agent_name=manifest.name,
        reversal_rate_target=round(ceiling, _RATIO_DECIMALS),
    )
