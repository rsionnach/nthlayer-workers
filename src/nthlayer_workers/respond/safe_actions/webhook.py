"""Webhook dispatcher for safe action execution bindings.

Resolves ``${ENV_VAR}`` secrets in the operator-supplied binding config,
then renders ``{{variable}}`` placeholders with incident-supplied
variables, then dispatches an HTTP call to an allowlisted host
(opensrm-9uow.2). Optionally verifies the result via PromQL.

Order is load-bearing: secrets are resolved BEFORE template rendering
so that an incident-supplied variable value of ``${ANYTHING}`` is left
as a literal in the rendered output rather than re-resolved as a
secret reference.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# Pattern matching a residual ``${VAR}`` reference. Used both to detect
# pre-render injection attempts and to scrub secrets from response text.
_SECRET_REF_PATTERN = re.compile(r"\$\{(\w+)\}")

# Env var listing comma-separated allowed webhook hosts (host or host:port).
# When unset, all webhook calls fail closed unless an allowlist is passed
# explicitly to ``WebhookDispatcher``.
_ALLOWLIST_ENV = "NTHLAYER_WEBHOOK_ALLOWLIST"


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


def _parse_allowlist(raw: str | None) -> set[str]:
    """Parse a comma-separated allowlist string into a set of host[:port]."""
    if not raw:
        return set()
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def _host_is_allowlisted(url: str, allowlist: set[str]) -> bool:
    """Return True iff the URL's host[:port] is in ``allowlist``."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if not parsed.hostname:
        return False
    candidate = parsed.hostname.lower()
    if parsed.port is not None:
        candidate_with_port = f"{candidate}:{parsed.port}"
        return candidate in allowlist or candidate_with_port in allowlist
    return candidate in allowlist


def _has_unresolved_secret_ref(value: Any) -> bool:
    """Return True if any string in the structure contains ``${VAR}``."""
    if isinstance(value, str):
        return bool(_SECRET_REF_PATTERN.search(value))
    if isinstance(value, dict):
        return any(_has_unresolved_secret_ref(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_unresolved_secret_ref(item) for item in value)
    return False


class WebhookDispatcher:
    """Execute safe action bindings via HTTP webhooks (opensrm-9uow.2).

    Hardening:

    - URLs are checked against an allowlist of host[:port] entries; any
      URL whose host is not allowlisted is rejected before the network
      call. Default allowlist is empty (fail-closed); operators
      configure via ``NTHLAYER_WEBHOOK_ALLOWLIST`` env var or by
      passing ``allowlist=`` to the constructor.
    - Variables (incident-supplied) are validated to contain no
      ``${VAR}`` references before substitution. Secrets are resolved
      from the binding config (operator-supplied) BEFORE template
      rendering so an incident value cannot inject a secret reference.
    - Response bodies never appear in the result; the ``detail`` field
      carries only a generic success/failure message and HTTP status
      code, to keep webhook server output (which may echo request data
      including resolved secrets) out of downstream verdict metadata.
    """

    def __init__(self, allowlist: set[str] | None = None) -> None:
        if allowlist is None:
            allowlist = _parse_allowlist(os.environ.get(_ALLOWLIST_ENV))
        self._allowlist = allowlist

    async def execute(
        self, binding: dict | str, variables: dict[str, str]
    ) -> ExecutionResult:
        """Render templates, resolve secrets, dispatch, optionally verify.

        See class docstring for the secret-vs-template ordering and the
        allowlist contract.
        """
        if binding == "stub" or not binding:
            target = variables.get("service", variables.get("target", "unknown"))
            return ExecutionResult(
                success=True,
                detail=f"Stub execution for {target} (no binding configured).",
            )

        # Reject incident-supplied variables that look like secret references.
        # Variables flow into URLs and bodies via render_binding_templates,
        # and a value like ``${SOME_KEY}`` would then be visually
        # indistinguishable from a legitimate operator-supplied secret
        # reference if the order were reversed.
        for key, value in variables.items():
            if isinstance(value, str) and _SECRET_REF_PATTERN.search(value):
                return ExecutionResult(
                    success=False,
                    detail=f"variable {key!r} contains a forbidden ${{...}} reference",
                )

        # Resolve secrets in the operator-controlled binding FIRST.
        try:
            resolved = resolve_secrets(binding)
        except ValueError as exc:
            return ExecutionResult(success=False, detail=str(exc))

        # Render incident-supplied variables. Any residual ``${...}`` in
        # the output is left as a literal — never re-resolved.
        rendered = render_binding_templates(resolved, variables)

        url = rendered.get("url", "")
        if not _host_is_allowlisted(url, self._allowlist):
            return ExecutionResult(
                success=False,
                detail=(
                    "webhook host not allowlisted (configure "
                    f"{_ALLOWLIST_ENV} env var)"
                ),
            )

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
        """Make HTTP POST with retry logic.

        Response bodies are NOT propagated into ``detail`` — a
        misconfigured echo-server-style webhook could otherwise reflect
        request data (including resolved secrets) back into downstream
        verdict metadata. ``detail`` carries only HTTP status code and
        attempt information.
        """
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
                            detail=f"webhook returned {resp.status_code}",
                        )
                    resp.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    last_error = f"HTTP {exc.response.status_code}"
                    last_status = exc.response.status_code
                except httpx.TimeoutException:
                    last_error = f"Timeout after {timeout}s"
                except Exception:
                    # Exception strings from httpx may include the URL or
                    # other request context. Use a generic message.
                    last_error = "webhook call failed"

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
