"""Tests for the worker module runner."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nthlayer_workers.runner import ModuleRunner, WorkerModule


@dataclass
class MockModule:
    """Test double implementing the WorkerModule protocol."""

    name: str = "test-module"
    cycle_count: int = 0
    restored_state: dict[str, Any] | None = None
    _state: dict[str, Any] = field(default_factory=dict)
    cycle_callback: Any = None  # optional async callback per cycle

    async def restore_state(self, state: dict[str, Any] | None) -> None:
        self.restored_state = state

    async def process_cycle(self) -> None:
        self.cycle_count += 1
        if self.cycle_callback:
            await self.cycle_callback()

    async def get_state(self) -> dict[str, Any]:
        return {"last_cursor": f"cycle-{self.cycle_count}", **self._state}


def _mock_api_result(ok=True, data=None):
    """Create a mock APIResult."""
    result = MagicMock()
    result.ok = ok
    result.data = data
    result.error = None if ok else "test_error"
    return result


class TestModuleRegistration:
    def test_register_module(self):
        runner = ModuleRunner()
        module = MockModule()
        runner.register(module, interval_seconds=30)
        assert len(runner._modules) == 1
        assert runner._modules[0].module.name == "test-module"
        assert runner._modules[0].interval_seconds == 30

    def test_register_multiple_modules(self):
        runner = ModuleRunner()
        runner.register(MockModule(name="observe"), interval_seconds=30)
        runner.register(MockModule(name="measure"), interval_seconds=60)
        assert len(runner._modules) == 2

    def test_mock_module_satisfies_protocol(self):
        assert isinstance(MockModule(), WorkerModule)


class TestRunnerCycle:
    @pytest.fixture
    def runner(self):
        r = ModuleRunner(core_url="http://test:8000", tick_interval=0.01)
        return r

    @pytest.fixture
    def mock_client(self):
        client = AsyncMock()
        client.get_component_state.return_value = _mock_api_result(ok=False)
        client.put_component_state.return_value = _mock_api_result()
        client.heartbeat.return_value = _mock_api_result()
        return client

    async def test_module_runs_at_interval(self, runner, mock_client):
        module = MockModule()
        runner.register(module, interval_seconds=0.01)

        async def stop_after_cycles():
            while module.cycle_count < 3:
                await asyncio.sleep(0.01)
            runner._shutdown = True

        runner.client = mock_client
        await asyncio.wait_for(
            asyncio.gather(runner.run(), stop_after_cycles()),
            timeout=5.0,
        )

        assert module.cycle_count >= 3

    async def test_heartbeat_emitted_per_cycle(self, runner, mock_client):
        module = MockModule()
        runner.register(module, interval_seconds=0.01)

        async def stop_after_one():
            while module.cycle_count < 1:
                await asyncio.sleep(0.01)
            runner._shutdown = True

        runner.client = mock_client
        await asyncio.wait_for(
            asyncio.gather(runner.run(), stop_after_one()),
            timeout=5.0,
        )

        mock_client.heartbeat.assert_called()
        call_kwargs = mock_client.heartbeat.call_args
        assert call_kwargs.kwargs["component"] == "workers"

    async def test_state_persisted_after_cycle(self, runner, mock_client):
        module = MockModule(name="observe")
        runner.register(module, interval_seconds=0.01)

        async def stop_after_one():
            while module.cycle_count < 1:
                await asyncio.sleep(0.01)
            runner._shutdown = True

        runner.client = mock_client
        await asyncio.wait_for(
            asyncio.gather(runner.run(), stop_after_one()),
            timeout=5.0,
        )

        mock_client.put_component_state.assert_called_with(
            "observe", {"last_cursor": "cycle-1"}
        )

    async def test_state_restored_on_startup(self, runner, mock_client):
        saved_state = {"last_cursor": "cycle-5", "hysteresis_state": {"x": 1}}
        mock_client.get_component_state.return_value = _mock_api_result(
            ok=True, data=saved_state
        )

        module = MockModule(name="measure")
        runner.register(module, interval_seconds=0.01)

        async def stop_immediately():
            await asyncio.sleep(0.02)
            runner._shutdown = True

        runner.client = mock_client
        await asyncio.wait_for(
            asyncio.gather(runner.run(), stop_immediately()),
            timeout=5.0,
        )

        assert module.restored_state == saved_state

    async def test_state_restored_as_none_when_no_prior_state(self, runner, mock_client):
        mock_client.get_component_state.return_value = _mock_api_result(ok=False)

        module = MockModule()
        runner.register(module, interval_seconds=0.01)

        async def stop_immediately():
            await asyncio.sleep(0.02)
            runner._shutdown = True

        runner.client = mock_client
        await asyncio.wait_for(
            asyncio.gather(runner.run(), stop_immediately()),
            timeout=5.0,
        )

        assert module.restored_state is None

    async def test_cycle_failure_does_not_crash_runner(self, runner, mock_client):
        module = MockModule(name="failing")
        call_count = 0

        async def fail_then_succeed():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("simulated failure")

        module.cycle_callback = fail_then_succeed
        runner.register(module, interval_seconds=0.01)

        async def stop_after_two():
            while module.cycle_count < 2:
                await asyncio.sleep(0.01)
            runner._shutdown = True

        runner.client = mock_client
        await asyncio.wait_for(
            asyncio.gather(runner.run(), stop_after_two()),
            timeout=5.0,
        )

        # Runner survived the failure and ran a second cycle
        assert module.cycle_count >= 2

    async def test_graceful_shutdown_persists_state(self, runner, mock_client):
        module = MockModule(name="correlate")
        runner.register(module, interval_seconds=0.01)

        async def stop_after_one():
            while module.cycle_count < 1:
                await asyncio.sleep(0.01)
            runner._shutdown = True

        runner.client = mock_client
        await asyncio.wait_for(
            asyncio.gather(runner.run(), stop_after_one()),
            timeout=5.0,
        )

        # State persisted during cycle + again during shutdown
        assert mock_client.put_component_state.call_count >= 2

    async def test_multiple_modules_different_intervals(self, runner, mock_client):
        fast = MockModule(name="fast")
        slow = MockModule(name="slow")
        runner.register(fast, interval_seconds=0.01)
        runner.register(slow, interval_seconds=0.05)

        async def stop_after_fast_runs():
            while fast.cycle_count < 3:
                await asyncio.sleep(0.01)
            runner._shutdown = True

        runner.client = mock_client
        await asyncio.wait_for(
            asyncio.gather(runner.run(), stop_after_fast_runs()),
            timeout=5.0,
        )

        # Fast module ran more times than slow
        assert fast.cycle_count >= 3
        assert slow.cycle_count >= 1
        assert fast.cycle_count > slow.cycle_count
