"""WebhookIngester — raw asyncio TCP HTTP server for event ingestion."""
from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime

from nthlayer_workers.correlate.ingestion import severity as _severity
from nthlayer_workers.correlate.types import EventType, SitRepEvent

_MAX_HEADER_SIZE = 65_536   # 64 KB
_MAX_BODY_SIZE = 10 * 1024 * 1024  # 10 MB
_CONNECTION_TIMEOUT = 30  # seconds — per-connection deadline to prevent slowloris

_REQUIRED_FIELDS = {"source", "type", "service", "payload"}


class WebhookIngester:
    """Accepts HTTP POST requests and dispatches SitRepEvents to a registered handler."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8081,
        slo_targets: dict | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._slo_targets = slo_targets
        self._handler: Callable[[SitRepEvent], Awaitable[None]] | None = None
        self._server: asyncio.Server | None = None

    def on_event(self, handler: Callable[[SitRepEvent], Awaitable[None]]) -> None:
        """Register the async event handler called for each valid incoming event."""
        self._handler = handler

    async def start(self) -> None:
        """Start listening for connections."""
        self._server = await asyncio.start_server(
            self._handle_connection, self._host, self._port
        )

    async def stop(self) -> None:
        """Stop the server and wait for it to close."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _parse_body(self, body: bytes) -> SitRepEvent:
        """Parse, validate, and construct a SitRepEvent from raw JSON bytes."""
        data = json.loads(body)
        missing = _REQUIRED_FIELDS - set(data.keys())
        if missing:
            raise ValueError(f"Missing required fields: {sorted(missing)}")

        # Normalise event type
        raw_type = data["type"]
        try:
            event_type = EventType(raw_type)
        except ValueError:
            raise ValueError(f"Unknown event type: {raw_type!r}")

        # Auto-generate id and timestamp when absent
        event_id = data.get("id") or str(uuid.uuid4())
        timestamp = data.get("timestamp") or datetime.now(UTC).isoformat()

        event = SitRepEvent(
            id=event_id,
            timestamp=timestamp,
            source=data["source"],
            type=event_type,
            service=data["service"],
            environment=data.get("environment", ""),
            severity=max(0.0, min(1.0, float(data.get("severity", 0.5)))),
            payload=data["payload"],
            dependencies=data.get("dependencies", []),
            dependents=data.get("dependents", []),
            ttl=int(data.get("ttl", 86400)),
        )

        # Apply arithmetic severity pre-scoring if SLO targets are configured
        scored = _severity.pre_score(event, self._slo_targets)
        if scored != event.severity:
            event = replace(event, severity=scored)

        return event

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle a single HTTP connection with per-connection timeout."""
        try:
            await asyncio.wait_for(
                self._handle_request(reader, writer), timeout=_CONNECTION_TIMEOUT
            )
        except TimeoutError:
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
            # --- Read headers until \r\n\r\n ---
            header_data = b""
            while b"\r\n\r\n" not in header_data:
                chunk = await reader.read(4096)
                if not chunk:
                    return
                header_data += chunk
                if len(header_data) > _MAX_HEADER_SIZE:
                    writer.write(
                        b"HTTP/1.1 431 Request Header Fields Too Large\r\nContent-Length: 0\r\n\r\n"
                    )
                    await writer.drain()
                    return

            header_part, _, body_start = header_data.partition(b"\r\n\r\n")
            headers_text = header_part.decode("utf-8", errors="replace")
            lines = headers_text.split("\r\n")
            request_line = lines[0] if lines else ""

            # --- Parse Content-Length ---
            content_length = 0
            for line in lines[1:]:
                if line.lower().startswith("content-length:"):
                    content_length = int(line.split(":", 1)[1].strip())
                    break

            if content_length > _MAX_BODY_SIZE:
                writer.write(
                    b"HTTP/1.1 413 Payload Too Large\r\nContent-Length: 0\r\n\r\n"
                )
                await writer.drain()
                return

            # --- Read remaining body bytes ---
            body = body_start
            while len(body) < content_length:
                chunk = await reader.read(content_length - len(body))
                if not chunk:
                    break
                body += chunk

            # --- Method check ---
            if not request_line.startswith("POST"):
                writer.write(
                    b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n"
                )
                await writer.drain()
                return

            # --- Parse, validate, and dispatch ---
            try:
                event = self._parse_body(body)
            except (json.JSONDecodeError, ValueError, KeyError) as exc:
                error_body = json.dumps({"error": str(exc)}).encode()
                response = (
                    f"HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(error_body)}\r\n\r\n"
                ).encode() + error_body
                writer.write(response)
                await writer.drain()
                return

            # Call the registered handler (async or sync)
            if self._handler is not None:
                result = self._handler(event)
                if asyncio.iscoroutine(result):
                    await result

            ok_body = b'{"status":"ok"}'
            response = (
                f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(ok_body)}\r\n\r\n"
            ).encode() + ok_body
            writer.write(response)
            await writer.drain()

        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
