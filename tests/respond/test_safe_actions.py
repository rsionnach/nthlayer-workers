# tests/test_safe_actions.py
"""Tests for safe action registry."""
import pytest
from nthlayer_workers.respond.safe_actions.registry import SafeAction, SafeActionRegistry
from nthlayer_workers.respond.safe_actions.actions import register_builtin_actions
from nthlayer_workers.respond.types import IncidentContext, IncidentState


def make_context():
    return IncidentContext(
        id="INC-2026-0001", state=IncidentState.REMEDIATING,
        created_at="2026-03-19T10:00:00Z", updated_at="2026-03-19T10:00:00Z",
        trigger_source="nthlayer-correlate", trigger_verdict_ids=[], topology={},
    )


@pytest.fixture
def registry(tmp_path):
    return SafeActionRegistry(cooldown_store_path=str(tmp_path / "cooldown.db"))


def test_register_and_get(registry):
    action = SafeAction(
        name="test_action", description="A test", target_type="service",
        requires_approval=False, cooldown_seconds=60,
        handler=lambda t, c, **kw: {"success": True, "detail": "ok"},
    )
    registry.register(action)
    assert registry.get("test_action").name == "test_action"


def test_get_unknown_raises(registry):
    with pytest.raises(KeyError):
        registry.get("nonexistent")


def test_list_actions(registry):
    action = SafeAction(
        name="rollback", description="Rollback service", target_type="service",
        requires_approval=True, cooldown_seconds=300,
        handler=lambda t, c, **kw: {"success": True},
    )
    registry.register(action)
    actions = registry.list_actions()
    assert len(actions) == 1
    assert actions[0]["name"] == "rollback"
    assert "description" in actions[0]


async def test_execute_success(registry):
    async def handler(target, context, **kwargs):
        return {"success": True, "detail": f"rolled back {target}"}

    registry.register(SafeAction(
        name="rollback", description="Rollback", target_type="service",
        requires_approval=True, cooldown_seconds=0,
        handler=handler,
    ))
    result = await registry.execute("rollback", "payment-api", make_context())
    assert result["success"] is True


async def test_execute_unknown_action(registry):
    with pytest.raises(KeyError):
        await registry.execute("nonexistent", "target", make_context())


async def test_cooldown_enforcement(registry):
    call_count = 0

    async def handler(target, context, **kwargs):
        nonlocal call_count
        call_count += 1
        return {"success": True, "detail": "ok"}

    registry.register(SafeAction(
        name="rollback", description="Rollback", target_type="service",
        requires_approval=False, cooldown_seconds=3600,  # 1 hour
        handler=handler,
    ))
    await registry.execute("rollback", "payment-api", make_context())
    with pytest.raises(Exception, match="cooldown"):
        await registry.execute("rollback", "payment-api", make_context())
    assert call_count == 1


async def test_cooldown_different_targets(registry):
    async def handler(target, context, **kwargs):
        return {"success": True}

    registry.register(SafeAction(
        name="rollback", description="Rollback", target_type="service",
        requires_approval=False, cooldown_seconds=3600,
        handler=handler,
    ))
    await registry.execute("rollback", "service-a", make_context())
    result = await registry.execute("rollback", "service-b", make_context())
    assert result["success"] is True


def test_builtin_actions_registered(tmp_path):
    registry = SafeActionRegistry(cooldown_store_path=str(tmp_path / "cd.db"))
    register_builtin_actions(registry)
    names = [a["name"] for a in registry.list_actions()]
    assert "rollback" in names
    assert "scale_up" in names
    assert "disable_feature_flag" in names
    assert "reduce_autonomy" in names
    assert "pause_pipeline" in names


def test_approval_ratchet(tmp_path):
    """Actions with requires_approval=True cannot be downgraded."""
    registry = SafeActionRegistry(cooldown_store_path=str(tmp_path / "cd.db"))
    register_builtin_actions(registry)
    rollback = registry.get("rollback")
    assert rollback.requires_approval is True


async def test_blast_radius_check_passes(registry):
    async def handler(target, context, **kwargs):
        return {"success": True}

    registry.register(SafeAction(
        name="test_action", description="Test", target_type="service",
        requires_approval=False, cooldown_seconds=0,
        handler=handler,
        blast_radius_check=lambda target, ctx: True,
    ))
    result = await registry.execute("test_action", "svc", make_context())
    assert result["success"] is True


async def test_blast_radius_check_fails(registry):
    async def handler(target, context, **kwargs):
        return {"success": True}

    registry.register(SafeAction(
        name="test_action", description="Test", target_type="service",
        requires_approval=False, cooldown_seconds=0,
        handler=handler,
        blast_radius_check=lambda target, ctx: False,
    ))
    with pytest.raises(Exception, match="blast.?radius"):
        await registry.execute("test_action", "svc", make_context())
