"""Pure-logic walkthrough state machine for the interactive TUI (jmy.22).

No Textual import here. The Textual screen in _interactive_app.py is a
thin wrapper over these primitives so the state-transition logic is
testable without Textual fixtures.
"""
from __future__ import annotations

import dataclasses
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

import yaml

from nthlayer_workers.learn.recommendations import (
    Recommendation,
    SpecRecommendation,
)


@dataclass
class WalkthroughState:
    """In-flight state of the interactive walkthrough.

    Tracks which recs the operator has decided on, any inline
    modifications to proposed_value, and the cursor position. The plan
    itself is held read-only (deep-copied at construction) so modify
    operations don't mutate the source.
    """
    plan: SpecRecommendation
    index: int = 0
    accepted_ids: set[str] = field(default_factory=set)
    rejected_ids: set[str] = field(default_factory=set)
    modified_values: dict[str, Any] = field(default_factory=dict)
    last_error: str | None = None  # last modify parse error message; cleared on next action

    @classmethod
    def for_plan(cls, plan: SpecRecommendation) -> "WalkthroughState":
        return cls(plan=deepcopy(plan))

    @property
    def total(self) -> int:
        return len(self.plan.recommendations)

    @property
    def current(self) -> Recommendation | None:
        if not self.plan.recommendations or self.index < 0 or self.index >= self.total:
            return None
        return self.plan.recommendations[self.index]

    @property
    def progress(self) -> str:
        if self.total == 0:
            return "[empty plan]"
        return f"[{self.index + 1} of {self.total}]"


def accept(state: WalkthroughState) -> WalkthroughState:
    """Mark current rec accepted; advance to next."""
    rec = state.current
    if rec is None:
        return state
    state.accepted_ids.add(rec.id)
    state.rejected_ids.discard(rec.id)
    state.last_error = None
    return _advance(state)


def reject(state: WalkthroughState) -> WalkthroughState:
    """Mark current rec rejected; advance to next.

    Also drops any prior modification of this rec — `modified_values`
    tracks state that only matters for accepted recs. Keeping a stale
    entry would resurrect the modification if a future change iterated
    `modified_values` directly (today `finalize` gates on `accepted_ids`
    so the leak is invisible, but the invariant is worth keeping clean).
    """
    rec = state.current
    if rec is None:
        return state
    state.rejected_ids.add(rec.id)
    state.accepted_ids.discard(rec.id)
    state.modified_values.pop(rec.id, None)
    state.last_error = None
    return _advance(state)


def modify(state: WalkthroughState, new_value_yaml: str) -> WalkthroughState:
    """Parse new_value_yaml; if valid, store modification + auto-accept the rec.

    Modify implies accept (operator wouldn't edit a rec they were going to
    reject). On parse failure, state is unchanged except for last_error
    which the screen renders.

    Empty or whitespace-only input is rejected: ``yaml.safe_load("")``
    legally returns ``None``, but silently overwriting proposed_value
    with None when the operator pressed Enter accidentally is a footgun
    (opensrm-jmy.22 P3 R5). Treat None and bare strings as a parse error.
    """
    rec = state.current
    if rec is None:
        return state
    if not new_value_yaml or not new_value_yaml.strip():
        state.last_error = (
            "modify requires a non-empty value; press Esc to cancel"
        )
        return state
    try:
        parsed = yaml.safe_load(new_value_yaml)
    except yaml.YAMLError as exc:
        state.last_error = f"YAML parse error: {exc}"
        return state
    if parsed is None:
        state.last_error = (
            "modify requires a non-null value; press Esc to cancel"
        )
        return state
    state.modified_values[rec.id] = parsed
    state.accepted_ids.add(rec.id)
    state.rejected_ids.discard(rec.id)
    state.last_error = None
    return _advance(state)


def next_rec(state: WalkthroughState) -> WalkthroughState:
    state.last_error = None
    return _advance(state)


def prev_rec(state: WalkthroughState) -> WalkthroughState:
    state.last_error = None
    if state.index > 0:
        state.index -= 1
    return state


def _advance(state: WalkthroughState) -> WalkthroughState:
    """Bump index by one, clamping at total (final position == done)."""
    if state.index < state.total:
        state.index += 1
    return state


def finalize(state: WalkthroughState) -> SpecRecommendation:
    """Return a new SpecRecommendation with only accepted recs and any
    modifications applied. Original plan is untouched.

    Recommendations preserve original plan order (NOT acceptance order).
    """
    new_recs = []
    for rec in state.plan.recommendations:
        if rec.id not in state.accepted_ids:
            continue
        if rec.id in state.modified_values:
            new_rec = dataclasses.replace(
                rec, proposed_value=state.modified_values[rec.id],
            )
        else:
            new_rec = rec
        new_recs.append(new_rec)
    return dataclasses.replace(state.plan, recommendations=new_recs)
