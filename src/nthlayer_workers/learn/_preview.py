"""Preview generation for the Learn → Spec workflow (jmy.6 § 6.1).

Pure functions. Given a recommendation + the manifest's current value
at the target path, produce a unified-diff-style preview string.
Operators see this in plan.yaml's `preview` field when --output is
called with --specs-dir.

When manifest's current value matches the recommendation's proposed
value, preview is empty (caller suppresses the field).
"""
from __future__ import annotations

from typing import Any

from nthlayer_workers.learn.recommendations import Recommendation


def build_preview(
    *,
    manifest_path: str,
    rec: Recommendation,
    manifest_current_value: Any,
) -> str:
    """Generate the per-recommendation preview field string.

    Empty string when manifest already matches the proposed value (no
    diff to show). Caller is responsible for omitting the field from
    plan.yaml when this returns empty.

    Drift marker is appended when manifest_current_value differs from
    rec.current_value — operators need to know the recommendation may
    no longer apply cleanly.
    """
    from nthlayer_workers.learn._yaml import normalize_scalar

    # Suppress preview if manifest is already at proposed state
    if normalize_scalar(manifest_current_value) == normalize_scalar(rec.proposed_value):
        return ""

    lines = [
        f"# File: {manifest_path}",
        f"# Path: {rec.field}",
    ]

    # Drift marker for operator visibility
    if rec.current_value is not None and \
       normalize_scalar(manifest_current_value) != normalize_scalar(rec.current_value):
        lines.append(
            f"# WARN: manifest drifted from recommendation's expected value "
            f"(current={manifest_current_value!r}, expected={rec.current_value!r})"
        )

    # Render the diff. Path leaf is the last dotted segment; indent matches
    # YAML 4-space-for-sequence-element style.
    leaf = rec.field.rsplit(".", 1)[-1] if rec.field else "(root)"

    if rec.current_value is None:
        # Adding new — show the proposed value as a new block
        lines.append(f"+   {leaf}:")
        for sub_line in _render_block(rec.proposed_value, indent=6):
            lines.append("+ " + sub_line)
    else:
        # Modifying existing
        lines.append(f"-   {leaf}: {_render_inline(rec.current_value)}")
        lines.append(f"+   {leaf}: {_render_inline(rec.proposed_value)}")

    return "\n".join(lines) + "\n"


def _render_inline(value: Any) -> str:
    """Render a scalar inline (matches YAML output for the common case)."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _render_block(value: Any, *, indent: int) -> list[str]:
    """Render a dict/list value as multi-line YAML-ish block."""
    pad = " " * indent
    if isinstance(value, dict):
        return [f"{pad}{k}: {_render_inline(v)}" for k, v in value.items()]
    if isinstance(value, list):
        return [f"{pad}- {_render_inline(v)}" for v in value]
    return [f"{pad}{_render_inline(value)}"]
