"""Textual carousel TUI for interactively walking SpecRecommendation
items (jmy.22).

Thin wrapper: state lives in _interactive.WalkthroughState; every key
binding maps to a pure transition function from that module. Run via
``run_walkthrough(plan) -> SpecRecommendation`` which constructs the
app, runs it, and returns the finalized plan.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import structlog
import yaml
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Input, Static

from nthlayer_workers.learn._apply import resolve_manifest_path
from nthlayer_workers.learn._interactive import (
    WalkthroughState,
    accept,
    finalize,
    modify,
    next_rec,
    prev_rec,
    reject,
)
from nthlayer_workers.learn._preview import build_preview
from nthlayer_workers.learn.recommendations import SpecRecommendation

log = structlog.get_logger(__name__)


class InteractiveWalkthroughApp(App[SpecRecommendation]):
    """Textual app for walking a SpecRecommendation plan rec-by-rec."""

    BINDINGS = [
        Binding("a", "accept", "Accept"),
        Binding("r", "reject", "Reject"),
        Binding("m", "modify", "Modify"),
        Binding("n", "next", "Next"),
        Binding("p", "prev", "Prev"),
        Binding("q", "quit", "Quit & apply"),
    ]

    CSS = """
    #header { dock: top; height: 3; }
    #diff   { padding: 1 2; }
    #footer { dock: bottom; height: 1; }
    #error  { dock: bottom; height: 1; color: red; }
    Input   { dock: bottom; height: 3; }
    """

    def __init__(
        self,
        plan: SpecRecommendation,
        specs_dir: str | None = None,
    ) -> None:
        super().__init__()
        self._state = WalkthroughState.for_plan(plan)
        self._specs_dir = specs_dir  # for build_preview's manifest lookup
        self._modify_mode = False

    def compose(self) -> ComposeResult:
        yield Static(id="header", markup=False)
        yield Static(id="diff", markup=False)
        yield Static(id="error", markup=False)
        # Modify input added/removed dynamically; placeholder via mount/remove

    def on_mount(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        rec = self._state.current
        header = self.query_one("#header", Static)
        diff = self.query_one("#diff", Static)
        error = self.query_one("#error", Static)
        if rec is None:
            header.update(f"Done {self._state.progress}")
            diff.update(self._summary())
        else:
            header.update(
                f"{self._state.progress}  {rec.id}  type={rec.type}  "
                f"service={rec.service}"
            )
            diff.update(self._render_diff(rec))
        error.update(self._state.last_error or "")

    def _render_diff(self, rec) -> str:
        """Use build_preview when specs_dir is available; otherwise
        render a minimal current→proposed block from rec fields alone."""
        if self._specs_dir:
            try:
                m_path = resolve_manifest_path(rec.service, Path(self._specs_dir))
                if m_path:
                    preview = build_preview(
                        manifest_path=str(m_path),
                        rec=rec,
                        manifest_current_value=rec.current_value,
                    )
                    if preview:
                        return f"{preview}\nRationale: {rec.rationale}"
            except Exception as exc:
                # Preview failed (manifest unreadable, path resolution edge
                # case, etc.) — fall through to the simple block. Log so
                # operators investigating a missing rich diff have a trace.
                log.warning(
                    "interactive_preview_failed",
                    rec_id=rec.id,
                    service=rec.service,
                    error=str(exc),
                )
        # Fallback: simple current → proposed block. When rec.field is
        # None (legitimate for placeholder recs like add_deploy_gate with
        # no breached SLO), key the YAML block by rec.type so the operator
        # still sees the proposed value rather than an empty diff
        # (opensrm-jmy.22 P1 R5).
        key = rec.field if rec.field else rec.type
        cur = (
            yaml.safe_dump({key: rec.current_value}, sort_keys=False)
            if rec.current_value is not None
            else ""
        )
        prop = yaml.safe_dump({key: rec.proposed_value}, sort_keys=False)
        return (
            f"--- current ---\n{cur}\n"
            f"+++ proposed +++\n{prop}\n\n"
            f"Rationale: {rec.rationale}"
        )

    def _summary(self) -> str:
        return (
            f"Accepted: {len(self._state.accepted_ids)}\n"
            f"Rejected: {len(self._state.rejected_ids)}\n"
            "Press q to apply accepted set."
        )

    # --- Actions ---

    def action_accept(self) -> None:
        if self._modify_mode:
            return
        self._state = accept(self._state)
        self._refresh()

    def action_reject(self) -> None:
        if self._modify_mode:
            return
        self._state = reject(self._state)
        self._refresh()

    def action_modify(self) -> None:
        if self._modify_mode:
            return
        rec = self._state.current
        if rec is None:
            return
        self._modify_mode = True
        # Prefill with current proposed_value as YAML
        prefill = yaml.safe_dump(rec.proposed_value, sort_keys=False).strip()
        inp = Input(
            value=prefill,
            id="modify-input",
            placeholder="YAML; Enter to confirm, Esc to cancel",
        )
        self.mount(inp)
        inp.focus()

    def action_next(self) -> None:
        if self._modify_mode:
            return
        self._state = next_rec(self._state)
        self._refresh()

    def action_prev(self) -> None:
        if self._modify_mode:
            return
        self._state = prev_rec(self._state)
        self._refresh()

    def action_quit(self) -> None:
        if self._modify_mode:
            return
        self.exit(finalize(self._state))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "modify-input":
            return
        self._state = modify(self._state, event.value)
        # If the parse failed, last_error is set; stay in modify mode so
        # operator can correct the value. Otherwise exit modify mode.
        if self._state.last_error is None:
            self._modify_mode = False
            event.input.remove()
        self._refresh()

    def on_key(self, event) -> None:
        # Esc cancels modify
        if self._modify_mode and event.key == "escape":
            try:
                self.query_one("#modify-input", Input).remove()
            except Exception:
                pass
            self._modify_mode = False
            self._state.last_error = None
            self._refresh()


def run_walkthrough(
    plan: SpecRecommendation,
    specs_dir: str | None = None,
) -> SpecRecommendation:
    """Run the interactive walkthrough synchronously; return the
    finalized plan (only accepted recs, with any modifications applied).
    """
    app = InteractiveWalkthroughApp(plan, specs_dir=specs_dir)
    result = app.run()
    return result if result is not None else dataclasses.replace(
        plan, recommendations=[],
    )
