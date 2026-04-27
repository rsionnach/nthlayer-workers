"""Load OpenSRM specs from a directory and extract ServiceSLO pairs.

Uses the canonical manifest parser from nthlayer-common. The local
SLODefinition class is deleted — use nthlayer_common.manifest.models.SLODefinition.
"""

from __future__ import annotations

from pathlib import Path

from nthlayer_common.manifest import ManifestLoadError, load_manifest

from nthlayer_workers.observe.slo.collector import ServiceSLO


def load_specs(specs_dir: str | Path) -> list[ServiceSLO]:
    """Load OpenSRM specs from a directory and extract ServiceSLO pairs.

    Reads all .yaml/.yml files, skips non-manifest files silently.
    Returns a flat list of ServiceSLO across all specs.
    """
    specs_path = Path(specs_dir)
    if not specs_path.is_dir():
        raise ValueError(f"Specs directory does not exist: {specs_dir}")

    results: list[ServiceSLO] = []

    for path in sorted(specs_path.iterdir()):
        if path.suffix not in (".yaml", ".yml"):
            continue

        try:
            manifest = load_manifest(path, suppress_deprecation_warning=True)
        except (ManifestLoadError, FileNotFoundError, ValueError, OSError):
            continue

        for slo in manifest.slos:
            results.append(ServiceSLO(service=manifest.name, slo=slo))

    return results
