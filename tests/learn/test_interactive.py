"""Unit tests for the interactive TUI walkthrough pure logic (opensrm-jmy.22).

These tests target the pure-logic module nthlayer_workers.learn._interactive
(no Textual dependency). Each test exercises a single state-transition
function so the eventual Textual app is a thin shell over a well-covered
state machine.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _build_single_rec_plan(*, rec_id: str = "rec-aaaaaaaaaaaa",
                           proposed_value=98.5,
                           field: str = "spec.slos.judgment.target"):
    """Build a SpecRecommendation with one Recommendation."""
    from nthlayer_workers.learn.recommendations import (
        SpecRecommendation, Recommendation,
    )

    return SpecRecommendation(
        incident="inc-jmy22",
        generated_by="nthlayer-learn",
        generated_at=datetime(2026, 5, 29, tzinfo=timezone.utc),
        confidence=0.7,
        recommendations=[
            Recommendation(
                id=rec_id,
                service="fraud-detect",
                type="tighten_slo",
                rationale="test rationale",
                field=field,
                current_value=95.0,
                proposed_value=proposed_value,
            ),
        ],
    )


def _build_multi_rec_plan(rec_ids=("rec-a", "rec-b", "rec-c")):
    """Build a SpecRecommendation with N recommendations sharing one service."""
    from nthlayer_workers.learn.recommendations import (
        SpecRecommendation, Recommendation,
    )

    return SpecRecommendation(
        incident="inc-jmy22-multi",
        generated_by="nthlayer-learn",
        generated_at=datetime(2026, 5, 29, tzinfo=timezone.utc),
        confidence=0.7,
        recommendations=[
            Recommendation(
                id=rid,
                service="fraud-detect",
                type="tighten_slo",
                rationale=f"rationale for {rid}",
                field=f"spec.slos.{rid}.target",
                current_value=95.0,
                proposed_value=98.5,
            )
            for rid in rec_ids
        ],
    )


def _build_empty_plan():
    from nthlayer_workers.learn.recommendations import SpecRecommendation

    return SpecRecommendation(
        incident="inc-empty",
        generated_by="nthlayer-learn",
        generated_at=datetime(2026, 5, 29, tzinfo=timezone.utc),
        confidence=0.0,
        recommendations=[],
    )


# ---------------------------------------------------------------------------
# TestWalkthroughState — construction + read-only properties
# ---------------------------------------------------------------------------


class TestWalkthroughState:
    def test_for_plan_deep_copies(self):
        from nthlayer_workers.learn._interactive import WalkthroughState

        plan = _build_single_rec_plan()
        original_rationale = plan.recommendations[0].rationale

        state = WalkthroughState.for_plan(plan)
        state.plan.recommendations[0].rationale = "mutated"

        assert plan.recommendations[0].rationale == original_rationale

    def test_empty_plan_total_is_zero(self):
        from nthlayer_workers.learn._interactive import WalkthroughState

        plan = _build_empty_plan()
        state = WalkthroughState.for_plan(plan)

        assert state.total == 0
        assert state.current is None
        assert state.progress == "[empty plan]"

    def test_progress_string_format(self):
        from nthlayer_workers.learn._interactive import (
            WalkthroughState, accept,
        )

        plan = _build_multi_rec_plan()
        state = WalkthroughState.for_plan(plan)

        assert state.progress == "[1 of 3]"

        state = accept(state)
        assert state.progress == "[2 of 3]"

    def test_current_is_none_at_end(self):
        from nthlayer_workers.learn._interactive import (
            WalkthroughState, accept,
        )

        plan = _build_single_rec_plan()
        state = WalkthroughState.for_plan(plan)
        state = accept(state)

        assert state.current is None


# ---------------------------------------------------------------------------
# TestAccept
# ---------------------------------------------------------------------------


class TestAccept:
    def test_accept_adds_to_accepted_ids(self):
        from nthlayer_workers.learn._interactive import (
            WalkthroughState, accept,
        )

        plan = _build_single_rec_plan(rec_id="rec-accept-1")
        state = WalkthroughState.for_plan(plan)
        state = accept(state)

        assert "rec-accept-1" in state.accepted_ids
        assert "rec-accept-1" not in state.rejected_ids

    def test_accept_advances_index(self):
        from nthlayer_workers.learn._interactive import (
            WalkthroughState, accept,
        )

        plan = _build_multi_rec_plan()
        state = WalkthroughState.for_plan(plan)
        assert state.index == 0

        state = accept(state)
        assert state.index == 1

    def test_accept_after_reject_swaps_buckets(self):
        from nthlayer_workers.learn._interactive import (
            WalkthroughState, accept, reject, prev_rec,
        )

        plan = _build_single_rec_plan(rec_id="rec-swap")
        state = WalkthroughState.for_plan(plan)
        state = reject(state)
        state = prev_rec(state)
        state = accept(state)

        assert "rec-swap" in state.accepted_ids
        assert "rec-swap" not in state.rejected_ids


# ---------------------------------------------------------------------------
# TestReject
# ---------------------------------------------------------------------------


class TestReject:
    def test_reject_adds_to_rejected_ids(self):
        from nthlayer_workers.learn._interactive import (
            WalkthroughState, reject,
        )

        plan = _build_single_rec_plan(rec_id="rec-reject-1")
        state = WalkthroughState.for_plan(plan)
        state = reject(state)

        assert "rec-reject-1" in state.rejected_ids
        assert "rec-reject-1" not in state.accepted_ids

    def test_reject_clears_accepted(self):
        from nthlayer_workers.learn._interactive import (
            WalkthroughState, accept, reject, prev_rec,
        )

        plan = _build_single_rec_plan(rec_id="rec-flip")
        state = WalkthroughState.for_plan(plan)
        state = accept(state)
        state = prev_rec(state)
        state = reject(state)

        assert "rec-flip" in state.rejected_ids
        assert "rec-flip" not in state.accepted_ids


# ---------------------------------------------------------------------------
# TestModify
# ---------------------------------------------------------------------------


class TestModify:
    def test_modify_scalar_yaml_stores_parsed_value(self):
        from nthlayer_workers.learn._interactive import (
            WalkthroughState, modify,
        )

        plan = _build_single_rec_plan(rec_id="rec-mod-scalar",
                                      proposed_value=98.5)
        state = WalkthroughState.for_plan(plan)
        state = modify(state, "99.0")

        assert state.modified_values["rec-mod-scalar"] == 99.0
        # Crucially: a float, not the string "99.0".
        assert isinstance(state.modified_values["rec-mod-scalar"], float)

    def test_modify_dict_yaml_stores_parsed_dict(self):
        from nthlayer_workers.learn._interactive import (
            WalkthroughState, modify,
        )

        plan = _build_single_rec_plan(
            rec_id="rec-mod-dict",
            proposed_value={"name": "svc-y", "type": "unknown"},
        )
        state = WalkthroughState.for_plan(plan)
        state = modify(state, "name: svc-z\ntype: api\n")

        assert state.modified_values["rec-mod-dict"] == {
            "name": "svc-z",
            "type": "api",
        }

    def test_modify_invalid_yaml_keeps_state_unchanged_sets_error(self):
        from nthlayer_workers.learn._interactive import (
            WalkthroughState, modify,
        )

        plan = _build_single_rec_plan(rec_id="rec-mod-bad")
        state = WalkthroughState.for_plan(plan)
        before_index = state.index
        state = modify(state, "[unbalanced")

        assert state.last_error is not None
        assert "YAML parse error" in state.last_error
        assert state.modified_values == {}
        assert state.accepted_ids == set()
        assert state.index == before_index

    def test_modify_auto_accepts(self):
        from nthlayer_workers.learn._interactive import (
            WalkthroughState, modify,
        )

        plan = _build_single_rec_plan(rec_id="rec-mod-auto")
        state = WalkthroughState.for_plan(plan)
        state = modify(state, "99.0")

        assert "rec-mod-auto" in state.accepted_ids

    def test_modify_clears_last_error_on_success(self):
        from nthlayer_workers.learn._interactive import (
            WalkthroughState, modify, prev_rec,
        )

        plan = _build_single_rec_plan(rec_id="rec-mod-clear")
        state = WalkthroughState.for_plan(plan)
        state = modify(state, "[unbalanced")
        assert state.last_error is not None

        # Bad modify keeps index unchanged; rewind to ensure the same rec
        # is current, then a good modify should clear last_error.
        state = prev_rec(state)
        state = modify(state, "99.0")

        assert state.last_error is None


# ---------------------------------------------------------------------------
# TestNavigation
# ---------------------------------------------------------------------------


class TestNavigation:
    def test_next_advances_index(self):
        from nthlayer_workers.learn._interactive import (
            WalkthroughState, next_rec,
        )

        plan = _build_multi_rec_plan()
        state = WalkthroughState.for_plan(plan)
        state = next_rec(state)

        assert state.index == 1

    def test_next_caps_at_total(self):
        from nthlayer_workers.learn._interactive import (
            WalkthroughState, next_rec,
        )

        plan = _build_multi_rec_plan(rec_ids=("rec-a", "rec-b"))
        state = WalkthroughState.for_plan(plan)
        for _ in range(5):
            state = next_rec(state)

        assert state.index == state.total == 2

    def test_prev_decrements_index(self):
        from nthlayer_workers.learn._interactive import (
            WalkthroughState, next_rec, prev_rec,
        )

        plan = _build_multi_rec_plan()
        state = WalkthroughState.for_plan(plan)
        state = next_rec(state)
        state = prev_rec(state)

        assert state.index == 0

    def test_prev_floors_at_zero(self):
        from nthlayer_workers.learn._interactive import (
            WalkthroughState, prev_rec,
        )

        plan = _build_multi_rec_plan()
        state = WalkthroughState.for_plan(plan)
        state = prev_rec(state)

        assert state.index == 0

    def test_next_clears_last_error(self):
        from nthlayer_workers.learn._interactive import (
            WalkthroughState, modify, next_rec,
        )

        plan = _build_multi_rec_plan()
        state = WalkthroughState.for_plan(plan)
        state = modify(state, "[unbalanced")
        assert state.last_error is not None

        state = next_rec(state)
        assert state.last_error is None


# ---------------------------------------------------------------------------
# TestFinalize
# ---------------------------------------------------------------------------


class TestFinalize:
    def test_finalize_keeps_only_accepted(self):
        from nthlayer_workers.learn._interactive import (
            WalkthroughState, accept, reject, finalize,
        )

        plan = _build_multi_rec_plan(rec_ids=("rec-a", "rec-b", "rec-c"))
        state = WalkthroughState.for_plan(plan)
        state = accept(state)   # rec-a
        state = reject(state)   # rec-b
        state = accept(state)   # rec-c

        result = finalize(state)
        ids = [r.id for r in result.recommendations]
        assert ids == ["rec-a", "rec-c"]

    def test_finalize_applies_modifications(self):
        from nthlayer_workers.learn._interactive import (
            WalkthroughState, modify, finalize,
        )

        plan = _build_single_rec_plan(rec_id="rec-mod-apply",
                                      proposed_value=98.5)
        state = WalkthroughState.for_plan(plan)
        state = modify(state, "99.5")

        result = finalize(state)
        assert len(result.recommendations) == 1
        assert result.recommendations[0].proposed_value == 99.5

    def test_finalize_preserves_plan_order_not_acceptance_order(self):
        from nthlayer_workers.learn._interactive import (
            WalkthroughState, accept, next_rec, prev_rec, finalize,
        )

        plan = _build_multi_rec_plan(rec_ids=("rec-a", "rec-b", "rec-c"))
        state = WalkthroughState.for_plan(plan)

        # Navigate to rec-c, accept it first.
        state = next_rec(state)   # → rec-b
        state = next_rec(state)   # → rec-c
        state = accept(state)     # accepts rec-c, index advances to 3

        # Rewind to rec-a, accept it second.
        state = prev_rec(state)   # → 2
        state = prev_rec(state)   # → 1
        state = prev_rec(state)   # → 0
        state = accept(state)     # accepts rec-a

        result = finalize(state)
        ids = [r.id for r in result.recommendations]
        # Plan order preserved even though rec-c was accepted first.
        assert ids == ["rec-a", "rec-c"]

    def test_finalize_empty_when_all_rejected(self):
        from nthlayer_workers.learn._interactive import (
            WalkthroughState, reject, finalize,
        )

        plan = _build_multi_rec_plan(rec_ids=("rec-a", "rec-b"))
        state = WalkthroughState.for_plan(plan)
        state = reject(state)
        state = reject(state)

        result = finalize(state)
        assert result.recommendations == []

    def test_finalize_does_not_mutate_source_plan(self):
        from nthlayer_workers.learn._interactive import (
            WalkthroughState, accept, modify, prev_rec, finalize,
        )

        plan = _build_multi_rec_plan(rec_ids=("rec-a", "rec-b"))
        original_count = len(plan.recommendations)
        original_proposed_values = [r.proposed_value for r in plan.recommendations]

        state = WalkthroughState.for_plan(plan)
        state = accept(state)               # rec-a accepted, index=1
        state = modify(state, "42.0")       # rec-b modified
        # Rewind so we can re-modify if needed — but here we just finalize.
        _ = finalize(state)
        # Also exercise rewind to ensure source plan is untouched in either path.
        state = prev_rec(state)
        _ = finalize(state)

        assert len(plan.recommendations) == original_count
        assert [r.proposed_value for r in plan.recommendations] == original_proposed_values


# ---------------------------------------------------------------------------
# jmy.22 P1 R5: _render_diff fallback when rec.field is None
# ---------------------------------------------------------------------------


class TestRenderDiffWithNoFieldPath:
    """jmy.22 P1 R5: a Recommendation with field=None (legitimate for
    placeholder recs) must still display proposed_value in the diff
    fallback. Previously the YAML block was keyed by rec.field and
    rendered empty when field was None.
    """

    def test_render_diff_uses_type_as_key_when_field_is_none(self):
        from nthlayer_workers.learn._interactive_app import InteractiveWalkthroughApp

        plan = _build_multi_rec_plan(rec_ids=("rec-a",))
        # Mutate the rec to drop field (simulating a placeholder add_deploy_gate
        # or any future rec type that legitimately has field=None).
        plan.recommendations[0] = dataclasses.replace(
            plan.recommendations[0],
            field=None,
            current_value=None,
            proposed_value={"action": "block", "service": "fraud-detect"},
        )

        app = InteractiveWalkthroughApp(plan, specs_dir=None)
        diff = app._render_diff(app._state.current)

        # proposed_value must be rendered somewhere in the diff text;
        # rec.type is used as the YAML key when field is None.
        assert "action" in diff
        assert "block" in diff
        assert "+++ proposed +++" in diff
