"""Webhook dispatcher for safe action execution bindings.

Renders {{variable}} templates, resolves ${ENV_VAR} secrets,
makes HTTP calls, and optionally verifies results via PromQL.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass
class ExecutionResult:
    """Result of a safe action execution."""

    success: bool
    status_code: int | None = None
    detail: str = ""
    verified: bool | None = None
    verification_detail: str | None = None


def render_binding_templates(obj: Any, variables: dict[str, str]) -> Any:
    """Recursively render {{variable}} placeholders in strings."""
    if isinstance(obj, str):
        for key, value in variables.items():
            obj = obj.replace("{{" + key + "}}", str(value))
            obj = obj.replace("{{ " + key + " }}", str(value))
        return obj
    if isinstance(obj, dict):
        return {k: render_binding_templates(v, variables) for k, v in obj.items()}
    if isinstance(obj, list):
        return [render_binding_templates(item, variables) for item in obj]
    return obj


def resolve_secrets(obj: Any) -> Any:
    """Recursively resolve ${ENV_VAR} placeholders from os.environ.

    Raises ValueError if a referenced env var is not set.
    """
    if isinstance(obj, str):
        def _replace(match):
            var_name = match.group(1)
            value = os.environ.get(var_name)
            if value is None:
                raise ValueError(
                    f"Secret ${{{var_name}}} not set. "
                    f"Set the {var_name} environment variable."
                )
            return value
        return re.sub(r'\$\{(\w+)\}', _replace, obj)
    if isinstance(obj, dict):
        return {k: resolve_secrets(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve_secrets(item) for item in obj]
    return obj


class WebhookDispatcher:
    """Execute safe action bindings via HTTP webhooks."""

    async def execute(
        self, binding: dict | str, variables: dict[str, str]
    ) -> ExecutionResult:
        """Render templates, resolve secrets, make HTTP call, verify."""
        if binding == "stub" or not binding:
            target = variables.get("service", variables.get("target", "unknown"))
            return ExecutionResult(
                success=True,
                detail=f"Stub execution for {target} (no binding configured).",
            )

        rendered = render_binding_templates(binding, variables)
        try:
            rendered = resolve_secrets(rendered)
        except ValueError as exc:
            return ExecutionResult(success=False, detail=str(exc))

        url = rendered.get("url", "")
        headers = rendered.get("headers", {})
        body = rendered.get("body")
        timeout = int(rendered.get("timeout", 30))
        retry_config = rendered.get("retry", {})
        verify_config = rendered.get("verify_after")

        result = await self._call_webhook(url, headers, body, timeout, retry_config)

        if verify_config and result.success:
            verification = await self._verify(verify_config, variables)
            result.verified = verification.verified
            result.verification_detail = verification.verification_detail

        return result

    async def _call_webhook(
        self, url, headers, body, timeout, retry_config
    ) -> ExecutionResult:
        """Make HTTP POST with retry logic."""
        attempts = retry_config.get("attempts", 1)
        backoff = retry_config.get("backoff", [1])
        last_error = ""
        last_status = None

        async with httpx.AsyncClient() as client:
            for attempt in range(attempts):
                try:
                    resp = await client.post(
                        url, headers=headers, json=body, timeout=timeout
                    )
                    last_status = resp.status_code
                    if resp.is_success:
                        return ExecutionResult(
                            success=True,
                            status_code=resp.status_code,
                            detail=resp.text[:500],
                        )
                    resp.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    last_error = f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"
                    last_status = exc.response.status_code
                except httpx.TimeoutException:
                    last_error = f"Timeout after {timeout}s"
                except Exception as exc:
                    last_error = str(exc)

                if attempt < attempts - 1:
                    delay = backoff[min(attempt, len(backoff) - 1)]
                    await asyncio.sleep(delay)

        return ExecutionResult(
            success=False, status_code=last_status, detail=last_error
        )

    async def _verify(self, verify_config, variables) -> ExecutionResult:
        """Wait, query Prometheus, return verification result."""
        wait = int(verify_config.get("wait", 30))
        query = verify_config.get("query", "")
        description = verify_config.get("description", "")
        prometheus_url = verify_config.get("prometheus_url") or os.environ.get(
            "PROMETHEUS_URL", "http://localhost:9090"
        )

        query = render_binding_templates(query, variables)

        logger.info("Waiting %ds before verification: %s", wait, description)
        await asyncio.sleep(wait)

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{prometheus_url}/api/v1/query",
                    params={"query": query},
                    timeout=10.0,
                )
                resp.raise_for_status()
                data = resp.json()
                results = data.get("data", {}).get("result", [])

                if not results:
                    return ExecutionResult(
                        success=True,
                        verified=None,
                        verification_detail=f"No data for query: {description}",
                    )

                value = float(results[0].get("value", [None, "0"])[1])
                verified = value == 1.0

                return ExecutionResult(
                    success=True,
                    verified=verified,
                    verification_detail=(
                        f"Verified: {description}"
                        if verified
                        else f"Verification failed: {description} (value={value})"
                    ),
                )

        except Exception as exc:
            logger.warning("Verification query failed: %s", exc)
            return ExecutionResult(
                success=True,
                verified=None,
                verification_detail=f"Verification unavailable: {exc}",
            )
