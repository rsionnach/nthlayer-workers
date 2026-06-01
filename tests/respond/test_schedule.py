"""Tests for on-call schedule resolver — pure function, no state."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from nthlayer_workers.respond.oncall.schedule import (
    OnCallResult,
    RosterMember,
    resolve_oncall,
)


def _make_config(
    *,
    roster_names: list[str] | None = None,
    rotation_type: str = "weekly",
    handoff: str = "monday 09:00",
    tz: str = "Europe/Dublin",
    overrides: list[dict] | None = None,
) -> dict:
    """Build a minimal oncall config dict for testing."""
    if roster_names is None:
        roster_names = ["Alice", "Bob", "Charlie"]
    roster = [
        {"name": n, "slack_id": f"U{i:04d}", "ntfy_topic": f"oncall-{n.lower()}"}
        for i, n in enumerate(roster_names)
    ]
    config = {
        "timezone": tz,
        "rotation": {
            "type": rotation_type,
            "handoff": handoff,
            "roster": roster,
        },
    }
    if overrides:
        config["overrides"] = overrides
    return config


class TestResolveOncallWeekly:
    """Test weekly rotation resolution."""

    def test_first_person_on_first_rotation(self):
        """First person in roster is primary right after handoff."""
        config = _make_config()
        # Monday 09:01 Dublin time — just after handoff
        tz = ZoneInfo("Europe/Dublin")
        now = datetime(2026, 4, 13, 9, 1, tzinfo=tz)  # Monday

        result = resolve_oncall(config, now)

        assert isinstance(result, OnCallResult)
        assert result.primary.name == "Alice"
        assert result.secondary.name == "Bob"
        assert result.source == "rotation"

    def test_second_person_after_one_week(self):
        """Second person after one full rotation period."""
        config = _make_config()
        tz = ZoneInfo("Europe/Dublin")
        # One week after first handoff
        now = datetime(2026, 4, 20, 9, 1, tzinfo=tz)  # Next Monday

        result = resolve_oncall(config, now)

        assert result.primary.name == "Bob"
        assert result.secondary.name == "Charlie"

    def test_rotation_wraps_around(self):
        """After all roster members, wraps back to first."""
        config = _make_config()
        tz = ZoneInfo("Europe/Dublin")
        # Three weeks after first handoff — back to Alice
        now = datetime(2026, 5, 4, 9, 1, tzinfo=tz)

        result = resolve_oncall(config, now)

        assert result.primary.name == "Alice"
        assert result.secondary.name == "Bob"

    def test_secondary_wraps_at_end_of_roster(self):
        """Secondary wraps to first person when primary is last."""
        config = _make_config()
        tz = ZoneInfo("Europe/Dublin")
        # Two weeks — Charlie is primary
        now = datetime(2026, 4, 27, 9, 1, tzinfo=tz)

        result = resolve_oncall(config, now)

        assert result.primary.name == "Charlie"
        assert result.secondary.name == "Alice"

    def test_handoff_time_returned(self):
        """Rotation handoff time is the end of primary's shift."""
        config = _make_config()
        tz = ZoneInfo("Europe/Dublin")
        now = datetime(2026, 4, 13, 12, 0, tzinfo=tz)  # Monday noon

        result = resolve_oncall(config, now)

        # Next handoff should be next Monday 09:00
        expected = datetime(2026, 4, 20, 9, 0, tzinfo=tz)
        assert result.rotation_handoff == expected


class TestResolveOncallDaily:
    """Test daily rotation resolution."""

    def test_daily_rotation_advances_each_day(self):
        """Daily rotation moves to next person each day."""
        config = _make_config(rotation_type="daily", handoff="09:00")
        tz = ZoneInfo("Europe/Dublin")

        # Day 0 after handoff
        now = datetime(2026, 4, 13, 10, 0, tzinfo=tz)
        result = resolve_oncall(config, now)
        assert result.primary.name == "Alice"

        # Day 1 after handoff
        now = datetime(2026, 4, 14, 10, 0, tzinfo=tz)
        result = resolve_oncall(config, now)
        assert result.primary.name == "Bob"

        # Day 2 after handoff
        now = datetime(2026, 4, 15, 10, 0, tzinfo=tz)
        result = resolve_oncall(config, now)
        assert result.primary.name == "Charlie"

    def test_daily_rotation_wraps(self):
        """Daily rotation wraps after exhausting roster."""
        config = _make_config(rotation_type="daily", handoff="09:00")
        tz = ZoneInfo("Europe/Dublin")
        # Day 3 — wraps back to Alice
        now = datetime(2026, 4, 16, 10, 0, tzinfo=tz)
        result = resolve_oncall(config, now)
        assert result.primary.name == "Alice"


class TestResolveOncallOverrides:
    """Test override resolution."""

    def test_override_takes_precedence(self):
        """Override user is primary when now is within override window."""
        config = _make_config(
            overrides=[
                {
                    "start": "2026-04-14T00:00:00+00:00",
                    "end": "2026-04-21T00:00:00+00:00",
                    "user": "Bob",
                    "reason": "Alice on leave",
                },
            ]
        )
        tz = ZoneInfo("Europe/Dublin")
        now = datetime(2026, 4, 15, 12, 0, tzinfo=tz)  # Within override window

        result = resolve_oncall(config, now)

        assert result.primary.name == "Bob"
        assert result.source == "override"

    def test_override_secondary_is_next_after_override_user(self):
        """Secondary is next person after override user in roster."""
        config = _make_config(
            overrides=[
                {
                    "start": "2026-04-14T00:00:00+00:00",
                    "end": "2026-04-21T00:00:00+00:00",
                    "user": "Bob",
                },
            ]
        )
        tz = ZoneInfo("Europe/Dublin")
        now = datetime(2026, 4, 15, 12, 0, tzinfo=tz)

        result = resolve_oncall(config, now)

        assert result.primary.name == "Bob"
        assert result.secondary.name == "Charlie"

    def test_override_boundary_start_inclusive(self):
        """Override is active at exactly the start time."""
        config = _make_config(
            overrides=[
                {
                    "start": "2026-04-14T00:00:00+00:00",
                    "end": "2026-04-21T00:00:00+00:00",
                    "user": "Charlie",
                },
            ]
        )
        now = datetime(2026, 4, 14, 0, 0, tzinfo=UTC)

        result = resolve_oncall(config, now)

        assert result.primary.name == "Charlie"
        assert result.source == "override"

    def test_override_boundary_end_exclusive(self):
        """Override is NOT active at exactly the end time."""
        config = _make_config(
            overrides=[
                {
                    "start": "2026-04-14T00:00:00+00:00",
                    "end": "2026-04-21T00:00:00+00:00",
                    "user": "Charlie",
                },
            ]
        )
        now = datetime(2026, 4, 21, 0, 0, tzinfo=UTC)

        result = resolve_oncall(config, now)

        # Should fall through to rotation, not override
        assert result.source == "rotation"

    def test_overlapping_overrides_first_match_wins(self):
        """When two overrides overlap, the first in the list wins."""
        config = _make_config(
            overrides=[
                {
                    "start": "2026-04-14T00:00:00+00:00",
                    "end": "2026-04-21T00:00:00+00:00",
                    "user": "Bob",
                },
                {
                    "start": "2026-04-15T00:00:00+00:00",
                    "end": "2026-04-18T00:00:00+00:00",
                    "user": "Charlie",
                },
            ]
        )
        tz = ZoneInfo("Europe/Dublin")
        now = datetime(2026, 4, 16, 12, 0, tzinfo=tz)  # Both overrides active

        result = resolve_oncall(config, now)

        assert result.primary.name == "Bob"  # First match wins
        assert result.source == "override"

    def test_no_override_outside_window(self):
        """When current time is outside override window, use rotation."""
        config = _make_config(
            overrides=[
                {
                    "start": "2026-04-14T00:00:00+00:00",
                    "end": "2026-04-21T00:00:00+00:00",
                    "user": "Bob",
                },
            ]
        )
        tz = ZoneInfo("Europe/Dublin")
        now = datetime(2026, 4, 22, 12, 0, tzinfo=tz)  # After override

        result = resolve_oncall(config, now)

        assert result.source == "rotation"


class TestResolveOncallEdgeCases:
    """Test edge cases."""

    def test_unknown_rotation_type_raises(self):
        """Unknown rotation type raises ValueError."""
        config = _make_config(rotation_type="biweekly")
        tz = ZoneInfo("Europe/Dublin")
        now = datetime(2026, 4, 13, 12, 0, tzinfo=tz)

        with pytest.raises(ValueError, match="Unknown rotation type"):
            resolve_oncall(config, now)

    def test_roster_member_fields(self):
        """RosterMember dataclass has expected fields."""
        member = RosterMember(
            name="Alice",
            slack_id="U0001",
            ntfy_topic="oncall-alice",
            phone="+353851234567",
        )
        assert member.name == "Alice"
        assert member.slack_id == "U0001"
        assert member.ntfy_topic == "oncall-alice"
        assert member.phone == "+353851234567"

    def test_roster_member_optional_fields_default_none(self):
        """RosterMember optional fields default to None."""
        member = RosterMember(name="Bob", slack_id="U0002")
        assert member.ntfy_topic is None
        assert member.phone is None

    def test_single_person_roster(self):
        """Single person roster: primary and secondary are the same."""
        config = _make_config(roster_names=["Alice"])
        tz = ZoneInfo("Europe/Dublin")
        now = datetime(2026, 4, 13, 12, 0, tzinfo=tz)

        result = resolve_oncall(config, now)

        assert result.primary.name == "Alice"
        assert result.secondary.name == "Alice"

    def test_utc_timezone(self):
        """Works with UTC timezone."""
        config = _make_config(tz="UTC")
        now = datetime(2026, 4, 13, 10, 0, tzinfo=UTC)

        result = resolve_oncall(config, now)

        assert isinstance(result, OnCallResult)
        assert result.primary.name is not None

    def test_empty_roster_raises(self):
        """Empty roster raises ValueError."""
        config = _make_config(roster_names=[])
        # Force empty roster
        config["rotation"]["roster"] = []
        tz = ZoneInfo("Europe/Dublin")
        now = datetime(2026, 4, 13, 12, 0, tzinfo=tz)

        with pytest.raises(ValueError, match="roster is empty"):
            resolve_oncall(config, now)

    def test_override_unknown_user_raises(self):
        """Override referencing a user not in roster raises ValueError."""
        config = _make_config(
            overrides=[
                {
                    "start": "2026-04-14T00:00:00+00:00",
                    "end": "2026-04-21T00:00:00+00:00",
                    "user": "Zara",  # Not in roster
                },
            ]
        )
        tz = ZoneInfo("Europe/Dublin")
        now = datetime(2026, 4, 15, 12, 0, tzinfo=tz)  # Within override window

        with pytest.raises(ValueError, match="unknown roster member"):
            resolve_oncall(config, now)

    def test_override_naive_datetime_coerced_to_schedule_tz(self):
        """Override with naive datetime strings is coerced to schedule timezone."""
        config = _make_config(
            overrides=[
                {
                    "start": "2026-04-14T00:00:00",  # No timezone
                    "end": "2026-04-21T00:00:00",
                    "user": "Bob",
                },
            ]
        )
        tz = ZoneInfo("Europe/Dublin")
        now = datetime(2026, 4, 15, 12, 0, tzinfo=tz)

        result = resolve_oncall(config, now)

        assert result.primary.name == "Bob"
        assert result.source == "override"

    def test_invalid_day_name_raises(self):
        """Invalid day name in handoff raises ValueError."""
        config = _make_config(handoff="mon 09:00")
        tz = ZoneInfo("Europe/Dublin")
        now = datetime(2026, 4, 13, 12, 0, tzinfo=tz)

        with pytest.raises(ValueError, match="Invalid day name"):
            resolve_oncall(config, now)

    def test_invalid_time_format_raises(self):
        """Invalid time format in handoff raises ValueError."""
        config = _make_config(handoff="monday 9am")
        tz = ZoneInfo("Europe/Dublin")
        now = datetime(2026, 4, 13, 12, 0, tzinfo=tz)

        with pytest.raises(ValueError, match="Invalid time"):
            resolve_oncall(config, now)

    def test_out_of_range_time_raises(self):
        """Out-of-range time values raise ValueError."""
        config = _make_config(handoff="monday 25:00")
        tz = ZoneInfo("Europe/Dublin")
        now = datetime(2026, 4, 13, 12, 0, tzinfo=tz)

        with pytest.raises(ValueError, match="Time out of range"):
            resolve_oncall(config, now)

    def test_now_exactly_at_handoff_time(self):
        """Resolves correctly when now is exactly at handoff time."""
        config = _make_config()
        tz = ZoneInfo("Europe/Dublin")
        now = datetime(2026, 4, 13, 9, 0, tzinfo=tz)  # Exactly 09:00 Monday

        result = resolve_oncall(config, now)

        assert isinstance(result, OnCallResult)
        assert result.source == "rotation"


# -- Cross-timezone resolution (opensrm-st4s.5) --

class TestResolveOncallCrossTimezone:
    """`now` in UTC must produce the correct rotation when config is in another zone."""

    def test_dublin_config_with_utc_now_deterministic(self):
        """Same instant produces identical primary regardless of how `now` is expressed."""
        config = _make_config(tz="Europe/Dublin", handoff="monday 09:00")
        # 2026-08-03 is a Monday. 11:00 UTC = 12:00 Dublin (BST).
        now_utc = datetime(2026, 8, 3, 11, 0, tzinfo=UTC)
        now_dublin = now_utc.astimezone(ZoneInfo("Europe/Dublin"))

        result_utc = resolve_oncall(config, now_utc)
        result_dublin = resolve_oncall(config, now_dublin)

        # Same instant → same primary/secondary. The resolver normalises
        # `now` into the configured timezone internally so both
        # representations are equivalent inputs.
        assert result_utc.primary.name == result_dublin.primary.name
        assert result_utc.secondary.name == result_dublin.secondary.name
        assert result_utc.source == "rotation"

    def test_nyc_config_with_utc_now_deterministic(self):
        """America/New_York config also consistent across `now` representations."""
        config = _make_config(tz="America/New_York", handoff="monday 09:00")
        now_utc = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)  # EDT 10:00
        now_nyc = now_utc.astimezone(ZoneInfo("America/New_York"))

        result_utc = resolve_oncall(config, now_utc)
        result_nyc = resolve_oncall(config, now_nyc)

        assert result_utc.primary.name == result_nyc.primary.name
        assert result_utc.secondary.name == result_nyc.secondary.name
        assert result_utc.source == "rotation"

    def test_handoff_returned_in_config_timezone(self):
        """rotation_handoff is returned in the configured timezone, not UTC."""
        config = _make_config(tz="Europe/Dublin", handoff="monday 09:00")
        now_utc = datetime(2026, 8, 3, 11, 0, tzinfo=UTC)
        result = resolve_oncall(config, now_utc)
        # ZoneInfo("Europe/Dublin") attaches; offset is +0100 (BST) in August.
        assert result.rotation_handoff.tzinfo is not None
        assert "Dublin" in str(result.rotation_handoff.tzinfo) or \
               result.rotation_handoff.utcoffset().total_seconds() != 0

    def test_dst_transition_handoff_lookup(self):
        """Handoff resolution still works around DST transitions.

        Europe/Dublin transitions to BST at 01:00 UTC on the last
        Sunday of March. The Monday after that should still resolve
        cleanly (the resolver uses _find_last_handoff in the local
        timezone — Dublin DST adjustments are handled by ZoneInfo).
        """
        config = _make_config(tz="Europe/Dublin", handoff="monday 09:00")
        # 2026-03-30 (post-DST-spring-forward) Monday 12:00 UTC.
        now_utc = datetime(2026, 3, 30, 12, 0, tzinfo=UTC)
        result = resolve_oncall(config, now_utc)
        assert result.source == "rotation"
        assert result.primary.name in {"Alice", "Bob", "Charlie"}
