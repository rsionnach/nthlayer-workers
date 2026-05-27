"""Apply orchestration for the Learn → Spec workflow (jmy.6 § 4 / § 5).

Reads target manifests via ruamel.yaml round-trip, classifies each
recommendation against current manifest state, deep-merges accepted
ones in memory, then writes all modified manifests atomically in a
final phase (alphabetical by path).

Resolution strategy: filename-convention first ('<specs-dir>/<svc>.yaml'
or '.yml'), recursive walk fallback finding manifests by metadata.name.
Hidden directories excluded from the walk.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import yaml as pyyaml  # lightweight read for the discovery walk only


def resolve_manifest_path(service: str, specs_dir: Path) -> Path | None:
    """Find the manifest file for a service in specs_dir.

    Strategy (per jmy.6 design § 4 Option B):
    1. Try <specs-dir>/<service>.yaml then <specs-dir>/<service>.yml
    2. If neither exists, recursive walk excluding hidden dirs;
       parse each .yaml/.yml and match metadata.name.

    Returns None if no manifest matches. Run is expected to handle
    None as manifest_not_found per jmy.6 § 7 Category B.
    """
    specs_dir = Path(specs_dir)

    # Convention attempt
    for ext in (".yaml", ".yml"):
        candidate = specs_dir / f"{service}{ext}"
        if candidate.is_file():
            return candidate

    # Walk fallback
    for path in _walk_yaml_files(specs_dir):
        try:
            text = path.read_text()
            data = pyyaml.safe_load(text)
        except (OSError, pyyaml.YAMLError):
            continue
        if not isinstance(data, dict):
            continue
        metadata = data.get("metadata") or {}
        if isinstance(metadata, dict) and metadata.get("name") == service:
            return path

    return None


def _walk_yaml_files(root: Path) -> Iterable[Path]:
    """Yield .yaml/.yml files under root, excluding hidden directories."""
    for path in root.rglob("*"):
        if path.is_dir():
            continue
        # Exclude any path with a hidden directory component
        if any(part.startswith(".") for part in path.relative_to(root).parts[:-1]):
            continue
        if path.suffix in (".yaml", ".yml"):
            yield path
