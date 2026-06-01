"""Worker module orchestrator.

Runs all configured worker modules on their specified intervals. Each module
implements the WorkerModule protocol: restore_state → process_cycle → persist_state.

One heartbeat emitted per runner cycle (not per module). State persisted to
core via the HTTP API after each cycle. Graceful shutdown on SIGTERM: finish
current cycle, persist state, then exit.

Usage:
    runner = ModuleRunner(core_url="http://localhost:8000")
    runner.register(observe_module, interval_seconds=30)
    runner.register(measure_module, interval_seconds=60)
    await runner.run()  # blocks until SIGTERM
"""

from __future__ import annotations

import asyncio
import signal
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import structlog
from nthlayer_common.api_client import CoreAPIClient

logger = structlog.get_logger()


@runtime_checkable
class WorkerModule(Protocol):
    """Protocol that all worker modules implement."""

    @property
    def name(self) -> str:
        """Module name (e.g. 'observe', 'measure')."""
        ...

    async def restore_state(self, state: dict[str, Any] | None) -> None:
        """Restore processing state from a previous run."""
        ...

    async def process_cycle(self) -> None:
        """Execute one processing cycle."""
        ...

    async def get_state(self) -> dict[str, Any]:
        """Return current state for persistence."""
        ...


@dataclass
class RegisteredModule:
    """A module registered with the runner."""

    module: WorkerModule
    interval_seconds: float
    last_run: float = 0.0


@dataclass
class ModuleRunner:
    """Orchestrates worker modules on configured intervals.

    Each tick:
      1. Check which modules are due to run
      2. Run due modules (sequentially within a tick)
      3. Persist state for each module that ran
      4. Emit one heartbeat to core (only on ticks where a module ran)

    Graceful shutdown: SIGTERM sets the shutdown flag. The current cycle
    completes, state is persisted, then the runner exits.
    """

    core_url: str = "http://localhost:8000"
    instance_id: str = "workers-1"
    tick_interval: float = 1.0  # seconds between scheduler ticks

    client: CoreAPIClient | None = None  # inject for testing; created lazily if None

    _modules: list[RegisteredModule] = field(default_factory=list, init=False)
    _shutdown: bool = field(default=False, init=False)

    def register(self, module: WorkerModule, interval_seconds: float) -> None:
        """Register a module to run at the specified interval."""
        self._modules.append(RegisteredModule(
            module=module,
            interval_seconds=interval_seconds,
        ))
        logger.info("module_registered", module=module.name, interval=interval_seconds)

    async def run(self) -> None:
        """Run the module loop until SIGTERM."""
        if self.client is None:
            self.client = CoreAPIClient(base_url=self.core_url)
        self._install_signal_handlers()

        logger.info("runner_starting", modules=[m.module.name for m in self._modules])

        # Restore state for all modules
        await self._restore_all_state()

        # Main loop
        while not self._shutdown:
            cycle_start = time.monotonic()
            ran_any = False

            for reg in self._modules:
                if self._shutdown:
                    break

                now = time.monotonic()
                if now - reg.last_run >= reg.interval_seconds:
                    await self._run_module(reg)
                    ran_any = True

            # Emit heartbeat if any module ran
            if ran_any:
                await self._emit_heartbeat()

            # Sleep until next tick
            elapsed = time.monotonic() - cycle_start
            sleep_time = max(0, self.tick_interval - elapsed)
            if sleep_time > 0 and not self._shutdown:
                await asyncio.sleep(sleep_time)

        # Graceful shutdown: persist state and emit final heartbeat
        logger.info("runner_shutting_down")
        await self._persist_all_state()
        await self._emit_heartbeat()
        logger.info("runner_stopped")

    async def _restore_all_state(self) -> None:
        """Restore state for all modules from core API."""
        for reg in self._modules:
            name = reg.module.name
            result = await self.client.get_component_state(name)
            state = result.data if result.ok else None
            try:
                await reg.module.restore_state(state)
                logger.info("state_restored", module=name, had_state=state is not None)
            except Exception:
                logger.exception("state_restore_failed", module=name)

    async def _run_module(self, reg: RegisteredModule) -> None:
        """Run a single module's processing cycle."""
        name = reg.module.name
        start = time.monotonic()

        try:
            await reg.module.process_cycle()
        except Exception:
            logger.exception("cycle_failed", module=name)
            reg.last_run = time.monotonic()
            return

        duration_ms = int((time.monotonic() - start) * 1000)
        logger.info("cycle_complete", module=name, duration_ms=duration_ms)

        try:
            state = await reg.module.get_state()
            await self.client.put_component_state(name, state)
        except Exception:
            logger.exception("state_persist_failed", module=name)

        reg.last_run = time.monotonic()

    async def _persist_all_state(self) -> None:
        """Persist state for all modules (shutdown path)."""
        for reg in self._modules:
            try:
                state = await reg.module.get_state()
                await self.client.put_component_state(reg.module.name, state)
            except Exception:
                logger.exception("shutdown_persist_failed", module=reg.module.name)

    async def _emit_heartbeat(self) -> None:
        """Emit a single heartbeat to core."""
        module_names = [r.module.name for r in self._modules]
        await self.client.heartbeat(
            component="workers",
            instance_id=self.instance_id,
            state={"modules": module_names},
        )

    def _install_signal_handlers(self) -> None:
        """Install SIGTERM/SIGINT handlers for graceful shutdown."""
        loop = asyncio.get_running_loop()

        def _handle_signal(signum: int) -> None:
            sig_name = signal.Signals(signum).name
            logger.info("signal_received", signal=sig_name)
            self._shutdown = True

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _handle_signal, sig)
