# tests/test_webhook.py
"""Tests for the webhook adapter — raw asyncio TCP HTTP server."""
from __future__ import annotations

import asyncio
import json

import pytest

from nthlayer_workers.measure.adapters.webhook import WebhookAdapter


def _make_request(method: str, body: bytes, extra_headers: str = "") -> bytes:
    """Build a minimal HTTP/1.1 request."""
    return (
        f"{method} / HTTP/1.1\r\n"
        f"Host: localhost\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"{extra_headers}"
        f"\r\n"
    ).encode() + body


async def _send_request(host: str, port: int, request: bytes) -> tuple[int, bytes]:
    """Open a TCP connection, send request, return (status_code, body)."""
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(request)
    await writer.drain()
    response = await asyncio.wait_for(reader.read(65536), timeout=5)
    writer.close()
    await writer.wait_closed()
    # Parse status code from first line
    first_line = response.split(b"\r\n", 1)[0].decode()
    status = int(first_line.split(" ", 2)[1])
    # Split headers from body
    if b"\r\n\r\n" in response:
        body = response.split(b"\r\n\r\n", 1)[1]
    else:
        body = b""
    return status, body


VALID_PAYLOAD = json.dumps({
    "agent_name": "test-agent",
    "task_id": "task-001",
    "output_content": "Hello, world",
    "output_type": "text",
}).encode()


@pytest.fixture
async def adapter():
    """Start a webhook adapter on a random port."""
    a = WebhookAdapter(host="127.0.0.1", port=0)
    server = await a.start_server()
    # Get the actual port assigned
    port = server.sockets[0].getsockname()[1]
    a._port = port
    await server.start_serving()
    yield a, port
    server.close()
    await server.wait_closed()


@pytest.mark.asyncio
async def test_valid_post_returns_200(adapter):
    a, port = adapter
    request = _make_request("POST", VALID_PAYLOAD)
    status, body = await _send_request("127.0.0.1", port, request)
    assert status == 200
    assert json.loads(body)["status"] == "ok"
    # Verify item was queued
    output = a._queue.get_nowait()
    assert output.agent_name == "test-agent"
    assert output.task_id == "task-001"


@pytest.mark.asyncio
async def test_get_returns_405(adapter):
    _, port = adapter
    request = _make_request("GET", b"")
    status, _ = await _send_request("127.0.0.1", port, request)
    assert status == 405


@pytest.mark.asyncio
async def test_missing_fields_returns_400(adapter):
    _, port = adapter
    body = json.dumps({"agent_name": "x"}).encode()
    request = _make_request("POST", body)
    status, resp_body = await _send_request("127.0.0.1", port, request)
    assert status == 400
    assert b"Missing required fields" in resp_body


@pytest.mark.asyncio
async def test_invalid_json_returns_400(adapter):
    _, port = adapter
    request = _make_request("POST", b"not json{{{")
    status, _ = await _send_request("127.0.0.1", port, request)
    assert status == 400


@pytest.mark.asyncio
async def test_oversized_body_returns_413(adapter):
    _, port = adapter
    # Claim a body bigger than 10 MB via Content-Length
    request = (
        f"POST / HTTP/1.1\r\n"
        f"Host: localhost\r\n"
        f"Content-Length: {11 * 1024 * 1024}\r\n"
        f"\r\n"
    ).encode()
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    response = await asyncio.wait_for(reader.read(65536), timeout=5)
    writer.close()
    await writer.wait_closed()
    assert b"413" in response


@pytest.mark.asyncio
async def test_oversized_headers_returns_431(adapter):
    _, port = adapter
    # Send a massive header block (> 64 KB)
    big_header = "X-Junk: " + "A" * 70_000 + "\r\n"
    request = _make_request("POST", b"{}", extra_headers=big_header)
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    response = await asyncio.wait_for(reader.read(65536), timeout=5)
    writer.close()
    await writer.wait_closed()
    assert b"431" in response


@pytest.mark.asyncio
async def test_queue_full_returns_503(adapter):
    a, port = adapter
    # Fill the queue
    from nthlayer_workers.measure.types import AgentOutput
    from datetime import datetime, timezone
    for i in range(1000):
        a._queue.put_nowait(AgentOutput(
            agent_name="filler", task_id=f"t-{i}",
            output_content="x", output_type="text",
            metadata={}, timestamp=datetime.now(timezone.utc),
        ))
    request = _make_request("POST", VALID_PAYLOAD)
    status, resp_body = await _send_request("127.0.0.1", port, request)
    assert status == 503
    assert b"queue full" in resp_body
