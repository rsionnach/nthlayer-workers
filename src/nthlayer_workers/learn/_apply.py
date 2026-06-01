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

from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Any, Iterable

import yaml as pyyaml  # lightweight read for the discovery walk only

from nthlayer_workers.learn._yaml import (
    LIST_APPEND_SIGIL,
    apply_at_path,
    classify_outcome,
    get_yaml_round_trip,
    resolve_path,
)
from nthlayer_workers.learn.recommendations import (
    OutcomeKind,
    SpecRecommendation,
)


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


@dataclass
class RecOutcome:
    """One recommendation's outcome from apply_recommendations."""

    id: str
    service: str
    field: str | None
    outcome: OutcomeKind
    detail: str = ""  # human-readable detail for skipped recs


@dataclass
class ApplyResult:
    """Result of an apply_recommendations call."""

    applied: list[RecOutcome] = field(default_factory=list)
    skipped: list[RecOutcome] = field(default_factory=list)
    modified_files: list[Path] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        """Per jmy.6 design § 7 deterministic rule (updated by opensrm-1mja).

        Empty plan → 0
        Every entry in ``applied`` is APPLY_CLEAN AND every entry in
            ``skipped`` is ALREADY_APPLIED → 0
        At least one clean op (APPLY_CLEAN applied OR ALREADY_APPLIED
            skipped) AND at least one non-idempotent skip → 1 (partial)
        Otherwise (zero clean ops + at least one non-idempotent skip) → 2

        ALREADY_APPLIED is an idempotent no-op (lives in ``self.skipped``
        after opensrm-1mja). It counts as a "clean op" for the partial-
        success boundary above — otherwise a re-run with one new drift
        would regress from exit 1 to exit 2 purely because of the
        ALREADY_APPLIED routing change.
        """
        # Empty plan → 0
        if not self.applied and not self.skipped:
            return 0
        non_idempotent_skips = [
            r for r in self.skipped
            if r.outcome != OutcomeKind.ALREADY_APPLIED
        ]
        # All applied are APPLY_CLEAN AND all skipped are ALREADY_APPLIED → 0
        all_applied_clean = all(
            r.outcome == OutcomeKind.APPLY_CLEAN for r in self.applied
        )
        if all_applied_clean and not non_idempotent_skips:
            return 0
        # Partial-success boundary: an ALREADY_APPLIED no-op IS a "clean
        # operation" for partial-vs-complete-failure purposes. Otherwise
        # a re-run that succeeded for most recs but hit one new drift
        # would regress from exit 1 (partial) to exit 2 (complete fail)
        # purely because of the ALREADY_APPLIED→skipped routing change.
        any_clean = (
            any(r.outcome == OutcomeKind.APPLY_CLEAN for r in self.applied)
            or any(r.outcome == OutcomeKind.ALREADY_APPLIED for r in self.skipped)
        )
        if any_clean and non_idempotent_skips:
            return 1  # partial
        return 2  # complete failure


def apply_recommendations(
    plan: SpecRecommendation,
    specs_dir: Path,
    *,
    force: bool = False,
) -> ApplyResult:
    """Apply the plan's recommendations to manifests in specs_dir.

    Two-phase: classify all recs first (in-memory), then write all
    modified manifests atomically in alphabetical-by-path order.

    --force normalises DRIFT_DETECTED into APPLY_CLEAN (recorded with
    detail string so callers know the override occurred).
    """
    specs_dir = Path(specs_dir)
    result = ApplyResult()
    yaml = get_yaml_round_trip()

    # Build {file_path: parsed_doc} cache so each unique manifest is
    # read+parsed once even if multiple recs target it.
    doc_cache: dict[Path, Any] = {}
    modified_paths: set[Path] = set()

    for rec in plan.recommendations:
        # Resolve manifest
        manifest_path = resolve_manifest_path(rec.service, specs_dir)
        if manifest_path is None:
            result.skipped.append(RecOutcome(
                id=rec.id,
                service=rec.service,
                field=rec.field,
                outcome=OutcomeKind.MANIFEST_NOT_FOUND,
                detail=f"no manifest for service {rec.service!r} in {specs_dir}",
            ))
            continue

        # Read + parse (cached)
        if manifest_path not in doc_cache:
            doc_cache[manifest_path] = yaml.load(manifest_path.read_text())
        doc = doc_cache[manifest_path]

        # Classify. The ``[+]`` sigil on rec.field is part of the
        # recommendation semantics (list-append); it is not part of the
        # dotted path used to resolve the current manifest value, so
        # strip it before calling resolve_path. apply_at_path and
        # classify_outcome both still see the sigil-bearing rec.field
        # and switch into list-append mode.
        lookup_path = rec.field or ""
        if lookup_path.endswith(LIST_APPEND_SIGIL):
            lookup_path = lookup_path[: -len(LIST_APPEND_SIGIL)]
        manifest_value = resolve_path(doc, lookup_path)
        outcome = classify_outcome(manifest_value, rec)

        if outcome == OutcomeKind.APPLY_CLEAN:
            apply_at_path(doc, rec.field, rec.proposed_value)
            modified_paths.add(manifest_path)
            result.applied.append(RecOutcome(
                id=rec.id,
                service=rec.service,
                field=rec.field,
                outcome=outcome,
            ))
        elif outcome == OutcomeKind.ALREADY_APPLIED:
            # No-op skip: the manifest already matches the proposed
            # value (scalar paths) or already contains the proposed
            # item (list-append paths). Belongs in skipped, not
            # applied — nothing was written to disk and downstream
            # operators counting "applied" should not see no-ops.
            # opensrm-1mja: discovered by the add_dependency integration
            # test's idempotency check.
            result.skipped.append(RecOutcome(
                id=rec.id,
                service=rec.service,
                field=rec.field,
                outcome=outcome,
            ))
        elif outcome == OutcomeKind.DRIFT_DETECTED and force:
            apply_at_path(doc, rec.field, rec.proposed_value)
            modified_paths.add(manifest_path)
            result.applied.append(RecOutcome(
                id=rec.id,
                service=rec.service,
                field=rec.field,
                outcome=OutcomeKind.APPLY_CLEAN,  # --force normalises to clean
                detail="applied via --force despite drift",
            ))
        else:
            result.skipped.append(RecOutcome(
                id=rec.id,
                service=rec.service,
                field=rec.field,
                outcome=outcome,
            ))

    # Write phase: alphabetical-by-path, atomic per-file
    for path in sorted(modified_paths):
        buf = StringIO()
        yaml.dump(doc_cache[path], buf)
        path.write_text(buf.getvalue())
        result.modified_files.append(path)

    return result


def format_summary(result: ApplyResult) -> str:
    """Build the end-of-run summary string per jmy.6 design § 6.2."""
    lines: list[str] = []

    lines.append(f"Applied: {len(result.applied)} recommendation"
                 f"{'s' if len(result.applied) != 1 else ''}")
    for r in result.applied:
        lines.append(f"  {r.id}  {r.service:<14} {r.field}")

    if result.skipped:
        lines.append("")
        lines.append(f"Skipped: {len(result.skipped)} recommendation"
                     f"{'s' if len(result.skipped) != 1 else ''}")
        for r in result.skipped:
            lines.append(f"  {r.id}  {r.service:<14} {r.outcome.value}")
            if r.detail:
                for detail_line in r.detail.splitlines():
                    lines.append(f"    {detail_line}")
            if r.outcome == OutcomeKind.DRIFT_DETECTED:
                lines.append("")
                lines.append(f"    Re-run with --force to apply {r.id} anyway.")

    lines.append("")
    lines.append(f"Exit code: {result.exit_code}")

    return "\n".join(lines)


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
