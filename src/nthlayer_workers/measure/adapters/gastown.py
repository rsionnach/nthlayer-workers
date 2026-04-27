"""GasTown adapter — polls bd for quality-review-result wisps.

Pure transport: translates bd wisp format into AgentOutput (ZFC).
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from nthlayer_workers.measure.adapters._util import BoundedSeenSet
from nthlayer_workers.measure.types import AgentOutput


class GasTownAdapter:
    """Polls bd for quality-review-result wisps and yields AgentOutput."""

    def __init__(
        self,
        rig_name: str,
        poll_interval: float = 60.0,
        bd_path: str = "bd",
    ) -> None:
        self._rig_name = rig_name
        self._poll_interval = poll_interval
        self._bd_path = bd_path
        self._seen = BoundedSeenSet()

    def name(self) -> str:
        return "gastown"

    async def receive(self) -> AsyncIterator[AgentOutput]:
        """Poll bd for new quality-review-result wisps."""
        while True:
            wisps = await self._query_wisps()
            for wisp in wisps:
                wisp_id = wisp.get("id", "")
                if wisp_id and wisp_id not in self._seen:
                    self._seen.add(wisp_id)
                    yield self._to_agent_output(wisp)
            await asyncio.sleep(self._poll_interval)

    async def _query_wisps(self) -> list[dict]:
        """Run bd list --json to fetch recent quality-review-result wisps.

        Uses create_subprocess_exec (not shell=True) to prevent injection.
        """
        proc = await asyncio.create_subprocess_exec(
            self._bd_path,
            "list",
            "--json",
            "--all",
            "-l",
            "type:plugin-run",
            "-l",
            "plugin:quality-review-result",
            "--created-after=-1h",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            return []
        try:
            return json.loads(stdout)
        except (json.JSONDecodeError, TypeError):
            return []

    @staticmethod
    def _to_agent_output(wisp: dict) -> AgentOutput:
        """Convert a bd wisp to AgentOutput. Pure transport."""
        labels: dict[str, str] = {}
        for label in wisp.get("labels", []):
            parts = label.split(":", 1)
            if len(parts) == 2:
                labels[parts[0]] = parts[1]

        return AgentOutput(
            agent_name=labels.get("worker", "unknown"),
            task_id=wisp.get("id", ""),
            output_content=wisp.get("description", ""),
            output_type="quality-review-result",
            metadata={
                "rig": labels.get("rig", ""),
                "score": labels.get("score", ""),
            },
        )
