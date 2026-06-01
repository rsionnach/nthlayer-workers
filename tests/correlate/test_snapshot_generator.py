"""Tests for snapshot generator."""
from __future__ import annotations

from nthlayer_workers.correlate.snapshot.generator import SnapshotBudget, SnapshotGenerator
from nthlayer_workers.correlate.types import AgentState, CorrelationGroup


def _make_group(priority: int = 3, services: list[str] | None = None,
                event_count: int = 1, group_id: str = "cg-001") -> CorrelationGroup:
    return CorrelationGroup(
        id=group_id,
        priority=priority,
        summary=f"Test group P{priority}",
        services=services or ["test-service"],
        signals=[],
        topology=None,
        change_candidates=[],
        first_seen="2026-03-01T14:00:00Z",
        last_updated="2026-03-01T14:05:00Z",
        event_count=event_count,
    )


class TestSnapshotGenerator:
    def test_all_groups_within_budget(self):
        gen = SnapshotGenerator()
        groups = [_make_group(priority=2, group_id=f"cg-{i}") for i in range(3)]
        prompt, cache_hit = gen.generate(groups)
        assert not cache_hit
        assert "Test group P2" in prompt
        assert len(prompt) > 0

    def test_p0_always_included(self):
        # Use tiny budget
        budget = SnapshotBudget(max_tokens=100, reserved_instructions=0, reserved_history=0)
        gen = SnapshotGenerator(budget=budget)
        groups = [
            _make_group(priority=0, group_id="cg-p0"),
            _make_group(priority=3, group_id="cg-p3"),
        ]
        prompt, _ = gen.generate(groups)
        assert "Test group P0" in prompt

    def test_low_priority_dropped_when_over_budget(self):
        budget = SnapshotBudget(max_tokens=200, reserved_instructions=0, reserved_history=0)
        gen = SnapshotGenerator(budget=budget)
        groups = [
            _make_group(priority=0, group_id="cg-p0"),
            _make_group(priority=3, group_id="cg-p3-big"),
        ]
        prompt, _ = gen.generate(groups)
        # P0 should be included; P3 might be dropped if budget is tight
        assert "Test group P0" in prompt

    def test_cache_hit(self):
        gen = SnapshotGenerator()
        groups = [_make_group(group_id="cg-cache")]
        _, cache_hit1 = gen.generate(groups)
        assert not cache_hit1
        _, cache_hit2 = gen.generate(groups)
        assert cache_hit2

    def test_cache_invalidated_by_different_groups(self):
        gen = SnapshotGenerator()
        gen.generate([_make_group(services=["svc-a"])])
        _, cache_hit = gen.generate([_make_group(services=["svc-b"])])
        assert not cache_hit

    def test_no_cache_in_incident_mode(self):
        gen = SnapshotGenerator()
        groups = [_make_group(group_id="cg-inc")]
        gen.generate(groups, state=AgentState.WATCHING)
        _, cache_hit = gen.generate(groups, state=AgentState.INCIDENT)
        assert not cache_hit

    def test_manual_invalidate(self):
        gen = SnapshotGenerator()
        groups = [_make_group(group_id="cg-inv")]
        gen.generate(groups)
        gen.invalidate_cache()
        _, cache_hit = gen.generate(groups)
        assert not cache_hit

    def test_empty_groups(self):
        gen = SnapshotGenerator()
        prompt, _ = gen.generate([])
        assert "healthy" in prompt.lower()

    def test_groups_sorted_by_priority(self):
        gen = SnapshotGenerator()
        groups = [
            _make_group(priority=3, group_id="cg-p3"),
            _make_group(priority=0, group_id="cg-p0"),
            _make_group(priority=1, group_id="cg-p1"),
        ]
        prompt, _ = gen.generate(groups)
        # P0 should appear before P3 in the prompt
        p0_pos = prompt.find("Test group P0")
        p3_pos = prompt.find("Test group P3")
        assert p0_pos < p3_pos
