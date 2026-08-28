"""Load OpenSRM specs from a directory and extract ServiceSLO pairs.

Uses the canonical manifest parser from nthlayer-common. The local
SLODefinition class is deleted — use nthlayer_common.manifest.models.SLODefinition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import structlog
from nthlayer_common.manifest import (
    ManifestLoadError,
    foreign_yaml_reason,
    iter_manifest_files,
    load_manifest,
)

from nthlayer_workers.observe.slo.collector import ServiceSLO

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class LoadedSpecs:
    """Outcome of scanning a specs directory (opensrm-3470).

    ``parse_failures`` counts FILES that failed to load while aiming to be a
    manifest — not services missing from ``service_slos``, and not foreign
    YAML sharing the directory.

    It travels with the SLOs because every downstream consumer evaluates
    ``service_slos`` alone: without the count, a service whose manifest
    failed to parse contributes zero SLOs and is indistinguishable from a
    service that declares none. The SLOs that would have breached are simply
    never evaluated, and nothing says so. The per-file
    ``spec_parse_failed`` warning carries which file and why.
    """

    service_slos: list[ServiceSLO] = field(default_factory=list)
    parse_failures: int = 0


def load_specs(specs_dir: str | Path) -> LoadedSpecs:
    """Load OpenSRM specs from a directory and extract ServiceSLO pairs.

    Reads every ``.yaml``/``.yml`` file. A file that fails to load is
    logged and counted when it was aiming to be a manifest, and recorded at
    debug when it plainly was not — a ``kustomization.yaml`` or a Prometheus
    rules file sharing the directory is not a broken manifest, and counting
    those would fire a coverage caveat on every mixed-directory run.

    The distinction is ``nthlayer_common.manifest.foreign_yaml_reason``,
    shared with the retrospective path (opensrm-oh27) rather than
    reimplemented here.

    Returns a :class:`LoadedSpecs` rather than a bare list, so callers can
    tell a partial view from a complete one.
    """
    specs_path = Path(specs_dir)
    if not specs_path.is_dir():
        raise ValueError(f"Specs directory does not exist: {specs_dir}")

    results: list[ServiceSLO] = []
    parse_failures = 0

    for path in iter_manifest_files(specs_path):
        try:
            manifest = load_manifest(path, suppress_deprecation_warning=True)
        except (ManifestLoadError, FileNotFoundError, ValueError, OSError) as exc:
            # The handler stays broad deliberately: ValueError covers both
            # UnicodeDecodeError on non-UTF-8 bytes and ReliabilityManifest's
            # own validation (an invalid tier or type), and a manifest
            # rejected for either reason is still a manifest that failed to
            # load. What changes is that it is no longer swallowed.
            reason = foreign_yaml_reason(path)
            if reason is not None:
                # Foreign YAML sharing the directory. Recorded rather than
                # dropped without trace — skipping without saying so is the
                # shape opensrm-3470 exists to remove.
                log.debug("spec_file_ignored", spec_file=str(path), reason=reason)
                continue
            parse_failures += 1
            log.warning("spec_parse_failed", spec_file=str(path), error=str(exc))
            continue

        for slo in manifest.slos:
            results.append(ServiceSLO(service=manifest.name, slo=slo))

    return LoadedSpecs(service_slos=results, parse_failures=parse_failures)
