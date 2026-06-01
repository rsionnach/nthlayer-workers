"""Tests for WebhookIngester."""
from __future__ import annotations

import asyncio
import json

import pytest

from nthlayer_workers.correlate.ingestion.webhook import WebhookIngester
from nthlayer_workers.correlate.types import EventType


class TestWebhookIngester:
    @pytest.mark.asyncio
    async def test_valid_event_received(self):
        received = []
        async def handler(event):
            received.append(event)

        ing = WebhookIngester(host="127.0.0.1", port=18081)
        ing.on_event(handler)
        await ing.start()

        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", 18081)
            body = json.dumps({
                "source": "prometheus",
                "type": "alert",
                "service": "payment-api",
                "environment": "production",
                "payload": {"alert_name": "test"}
            }).encode()
            request = (
                f"POST /events HTTP/1.1\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"Content-Type: application/json\r\n"
                f"\r\n"
            ).encode() + body
            writer.write(request)
            await writer.drain()
            response = await asyncio.wait_for(reader.read(4096), timeout=2.0)
            writer.close()
            await writer.wait_closed()

            assert b"200" in response
            assert len(received) == 1
            assert received[0].service == "payment-api"
            assert received[0].type == EventType.ALERT
            assert received[0].id  # auto-generated
            assert received[0].timestamp  # auto-generated
        finally:
            await ing.stop()

    @pytest.mark.asyncio
    async def test_missing_required_field_returns_400(self):
        ing = WebhookIngester(host="127.0.0.1", port=18082)
        ing.on_event(lambda e: None)
        await ing.start()

        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", 18082)
            body = json.dumps({"source": "test"}).encode()  # missing type, service, payload
            request = (
                f"POST /events HTTP/1.1\r\n"
                f"Content-Length: {len(body)}\r\n\r\n"
            ).encode() + body
            writer.write(request)
            await writer.drain()
            response = await asyncio.wait_for(reader.read(4096), timeout=2.0)
            writer.close()
            await writer.wait_closed()

            assert b"400" in response
        finally:
            await ing.stop()
