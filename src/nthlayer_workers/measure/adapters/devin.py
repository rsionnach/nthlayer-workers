"""Devin adapter — polls Devin REST API for completed sessions.

Pure transport: translates Devin session format into AgentOutput (ZFC).
Uses httpx (transitive dep via anthropic SDK) with lazy import.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator

from nthlayer_workers.measure.adapters._util import BoundedSeenSet
from nthlayer_workers.measure.types import AgentOutput

logger = logging.getLogger(__name__)


class DevinAdapter:
    """Polls Devin API for completed sessions and yields AgentOutput."""

    def __init__(
        self,
        api_key: str | None = None,
        api_key_env: str = "DEVIN_API_KEY",
        poll_interval: float = 30.0,
        base_url: str = "https://api.devin.ai",
    ) -> None:
        self._api_key = api_key or os.environ.get(api_key_env, "")
        self._poll_interval = poll_interval
        self._base_url = base_url.rstrip("/")
        self._seen = BoundedSeenSet()
        self._client = None

    def _get_client(self):
        import httpx

        if self._client is None:
            self._client = httpx.AsyncClient()
        return self._client

    async def close(self) -> None:
        """Close the httpx client if it was created."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def name(self) -> str:
        return "devin"

    async def receive(self) -> AsyncIterator[AgentOutput]:
        """Poll Devin API for completed sessions."""
        while True:
            sessions = await self._list_sessions()
            for session in sessions:
                sid = session.get("session_id", "")
                if sid and sid not in self._seen and self._is_complete(session):
                    self._seen.add(sid)
                    detail = await self._get_session(sid)
                    if detail is not None:
                        yield self._to_agent_output(detail)
            await asyncio.sleep(self._poll_interval)

    async def _list_sessions(self) -> list[dict]:
        """GET /v1/sessions."""
        import httpx

        client = self._get_client()
        try:
            resp = await client.get(
                f"{self._base_url}/v1/sessions",
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("sessions", data if isinstance(data, list) else [])
        except httpx.HTTPError:
            logger.warning("Failed to list Devin sessions", exc_info=True)
            return []

    async def _get_session(self, session_id: str) -> dict | None:
        """GET /v1/sessions/{id}. Returns None on failure."""
        import httpx

        client = self._get_client()
        try:
            resp = await client.get(
                f"{self._base_url}/v1/sessions/{session_id}",
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError:
            logger.warning("Failed to get Devin session %s", session_id, exc_info=True)
            return None

    @staticmethod
    def _is_complete(session: dict) -> bool:
        return session.get("status") in ("completed", "stopped", "failed")

    @staticmethod
    def _to_agent_output(session: dict) -> AgentOutput:
        """Convert Devin session to AgentOutput. Pure transport."""
        structured = session.get("structured_output")
        content = (
            json.dumps(structured) if structured else session.get("title", "")
        )
        return AgentOutput(
            agent_name=f"devin:{session.get('session_id', '')}",
            task_id=session.get("session_id", ""),
            output_content=content,
            output_type="devin-session",
            metadata={
                "status": session.get("status", ""),
                "created_at": session.get("created_at", ""),
            },
        )
