"""Webhook adapter — accepts HTTP POSTs and yields AgentOutput."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone

from nthlayer_workers.measure.types import AgentOutput

_MAX_HEADER_SIZE = 65_536  # 64 KB
_MAX_BODY_SIZE = 10 * 1024 * 1024  # 10 MB
_MAX_QUEUE_SIZE = 1000
_CONNECTION_TIMEOUT = 30  # seconds — per-connection deadline to prevent slowloris


class WebhookAdapter:
    """Receives agent output via HTTP webhook POST requests."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8080) -> None:
        self._host = host
        self._port = port
        self._queue: asyncio.Queue[AgentOutput] = asyncio.Queue(maxsize=_MAX_QUEUE_SIZE)
        self._server: asyncio.AbstractServer | None = None

    def name(self) -> str:
        return "webhook"

    def _parse_body(self, body: bytes) -> AgentOutput:
        """Parse and validate JSON body into AgentOutput. Pure transport."""
        data = json.loads(body)
        required = {"agent_name", "task_id", "output_content", "output_type"}
        missing = required - set(data.keys())
        if missing:
            raise ValueError(f"Missing required fields: {missing}")

        return AgentOutput(
            agent_name=data["agent_name"],
            task_id=data["task_id"],
            output_content=data["output_content"],
            output_type=data["output_type"],
            metadata=data.get("metadata", {}),
            timestamp=datetime.now(timezone.utc),
        )

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle a single HTTP connection — minimal HTTP parsing."""
        try:
            await asyncio.wait_for(
                self._handle_request(reader, writer), timeout=_CONNECTION_TIMEOUT
            )
        except asyncio.TimeoutError:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_request(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Inner request handler, called within a per-connection timeout."""
        try:
            # Read request line and headers with size limit
            header_data = b""
            while b"\r\n\r\n" not in header_data:
                chunk = await reader.read(4096)
                if not chunk:
                    return
                header_data += chunk
                if len(header_data) > _MAX_HEADER_SIZE:
                    response = b"HTTP/1.1 431 Request Header Fields Too Large\r\nContent-Length: 0\r\n\r\n"
                    writer.write(response)
                    await writer.drain()
                    return

            header_part, _, body_start = header_data.partition(b"\r\n\r\n")
            headers_text = header_part.decode("utf-8", errors="replace")
            lines = headers_text.split("\r\n")
            request_line = lines[0] if lines else ""

            # Parse content-length
            content_length = 0
            for line in lines[1:]:
                if line.lower().startswith("content-length:"):
                    content_length = int(line.split(":", 1)[1].strip())

            # Reject oversized bodies
            if content_length > _MAX_BODY_SIZE:
                response = b"HTTP/1.1 413 Payload Too Large\r\nContent-Length: 0\r\n\r\n"
                writer.write(response)
                await writer.drain()
                return

            # Read remaining body
            body = body_start
            while len(body) < content_length:
                chunk = await reader.read(content_length - len(body))
                if not chunk:
                    break
                body += chunk

            # Only accept POST
            if not request_line.startswith("POST"):
                response = b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n"
                writer.write(response)
                await writer.drain()
                return

            try:
                output = self._parse_body(body)
                try:
                    self._queue.put_nowait(output)
                except asyncio.QueueFull:
                    error_body = b'{"error":"queue full, try again later"}'
                    response = (
                        f"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\n"
                        f"Content-Length: {len(error_body)}\r\n\r\n"
                    ).encode() + error_body
                    writer.write(response)
                    await writer.drain()
                    return
                response = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 15\r\n\r\n{\"status\":\"ok\"}"
            except (json.JSONDecodeError, ValueError, KeyError) as exc:
                error_body = json.dumps({"error": str(exc)}).encode()
                response = (
                    f"HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(error_body)}\r\n\r\n"
                ).encode() + error_body

            writer.write(response)
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def start_server(self) -> asyncio.AbstractServer:
        """Start the HTTP server. Returns the server for lifecycle management."""
        server = await asyncio.start_server(
            self._handle_connection, self._host, self._port
        )
        self._server = server
        return server

    async def receive(self) -> AsyncIterator[AgentOutput]:
        """Yield validated AgentOutput from the queue (fed by HTTP handler)."""
        if self._server is None:
            server = await self.start_server()
        else:
            server = self._server
        async with server:
            await server.start_serving()
            while True:
                output = await self._queue.get()
                yield output
