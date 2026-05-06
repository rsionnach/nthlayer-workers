"""Tests for WebhookDispatcher — template rendering, secret resolution, execution."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from nthlayer_workers.respond.safe_actions.webhook import (
    WebhookDispatcher,
    render_binding_templates,
    resolve_secrets,
)


# --- Template rendering ---

class TestRenderTemplates:
    def test_renders_string_variables(self):
        result = render_binding_templates(
            "https://api.internal/{{service}}/rollback",
            {"service": "fraud-detect"},
        )
        assert result == "https://api.internal/fraud-detect/rollback"

    def test_renders_nested_dict(self):
        obj = {"url": "https://{{service}}", "body": {"target": "{{target}}"}}
        result = render_binding_templates(obj, {"service": "api", "target": "svc-1"})
        assert result == {"url": "https://api", "body": {"target": "svc-1"}}

    def test_missing_variable_left_as_is(self):
        result = render_binding_templates("{{missing}}", {})
        assert result == "{{missing}}"

    def test_renders_in_lists(self):
        result = render_binding_templates(["{{a}}", "{{b}}"], {"a": "1", "b": "2"})
        assert result == ["1", "2"]

    def test_non_string_passthrough(self):
        assert render_binding_templates(42, {}) == 42
        assert render_binding_templates(True, {}) is True


# --- Secret resolution ---

class TestResolveSecrets:
    def test_resolves_env_var(self, monkeypatch):
        monkeypatch.setenv("MY_TOKEN", "secret123")
        result = resolve_secrets("Bearer ${MY_TOKEN}")
        assert result == "Bearer secret123"

    def test_missing_env_var_raises(self, monkeypatch):
        monkeypatch.delenv("MISSING_VAR", raising=False)
        with pytest.raises(ValueError, match="MISSING_VAR"):
            resolve_secrets("${MISSING_VAR}")

    def test_nested_dict_resolution(self, monkeypatch):
        monkeypatch.setenv("TOKEN", "abc")
        obj = {"headers": {"Authorization": "Bearer ${TOKEN}"}}
        result = resolve_secrets(obj)
        assert result == {"headers": {"Authorization": "Bearer abc"}}

    def test_no_secrets_passthrough(self):
        assert resolve_secrets("no secrets here") == "no secrets here"


# --- WebhookDispatcher execution ---

class TestWebhookDispatcher:
    ALLOWLIST = {"api.internal"}

    @pytest.mark.asyncio
    async def test_successful_webhook_call(self, monkeypatch):
        monkeypatch.setenv("TEST_TOKEN", "tok")
        binding = {
            "method": "webhook",
            "url": "https://api.internal/{{service}}/action",
            "headers": {"Authorization": "Bearer ${TEST_TOKEN}"},
            "body": {"target": "{{service}}"},
            "timeout": 10,
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "OK"
        mock_resp.is_success = True
        mock_resp.raise_for_status = MagicMock()

        with patch("nthlayer_workers.respond.safe_actions.webhook.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            dispatcher = WebhookDispatcher(allowlist=self.ALLOWLIST)
            result = await dispatcher.execute(binding, {"service": "fraud-detect"})

        assert result.success is True
        assert result.status_code == 200
        call_url = mock_client.post.call_args[0][0]
        assert "fraud-detect" in call_url
        # Header secret was resolved before template rendering.
        call_headers = mock_client.post.call_args.kwargs["headers"]
        assert call_headers["Authorization"] == "Bearer tok"

    @pytest.mark.asyncio
    async def test_http_error_returns_failure(self):
        binding = {"url": "https://api.internal/action", "timeout": 5}

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_resp.is_success = False
        mock_resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "500", request=MagicMock(), response=mock_resp
            )
        )

        with patch("nthlayer_workers.respond.safe_actions.webhook.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            dispatcher = WebhookDispatcher(allowlist=self.ALLOWLIST)
            result = await dispatcher.execute(binding, {})

        assert result.success is False
        assert result.status_code == 500

    @pytest.mark.asyncio
    async def test_stub_binding_returns_stub_result(self):
        dispatcher = WebhookDispatcher()
        result = await dispatcher.execute("stub", {"service": "test"})
        assert result.success is True
        assert "stub" in result.detail.lower()


# --- opensrm-9uow.2 hardening ---

class TestAllowlist:
    """URLs whose host is not allowlisted are rejected before the network call."""

    @pytest.mark.asyncio
    async def test_default_allowlist_is_empty_fail_closed(self, monkeypatch):
        monkeypatch.delenv("NTHLAYER_WEBHOOK_ALLOWLIST", raising=False)
        dispatcher = WebhookDispatcher()
        binding = {"url": "https://api.internal/action"}
        with patch("nthlayer_workers.respond.safe_actions.webhook.httpx.AsyncClient") as mock_cls:
            result = await dispatcher.execute(binding, {})
            mock_cls.assert_not_called()  # never reached the network
        assert result.success is False
        assert "allowlist" in result.detail.lower()

    @pytest.mark.asyncio
    async def test_env_allowlist_picked_up(self, monkeypatch):
        monkeypatch.setenv("NTHLAYER_WEBHOOK_ALLOWLIST", "api.internal,other.example")
        dispatcher = WebhookDispatcher()
        assert "api.internal" in dispatcher._allowlist
        assert "other.example" in dispatcher._allowlist

    @pytest.mark.asyncio
    async def test_non_allowlisted_host_rejected(self):
        dispatcher = WebhookDispatcher(allowlist={"api.internal"})
        binding = {"url": "https://attacker.example/action"}
        with patch("nthlayer_workers.respond.safe_actions.webhook.httpx.AsyncClient") as mock_cls:
            result = await dispatcher.execute(binding, {})
            mock_cls.assert_not_called()
        assert result.success is False

    @pytest.mark.asyncio
    async def test_allowlist_blocks_loopback_unless_listed(self):
        dispatcher = WebhookDispatcher(allowlist={"api.internal"})
        binding = {"url": "http://127.0.0.1:8080/admin"}
        with patch("nthlayer_workers.respond.safe_actions.webhook.httpx.AsyncClient") as mock_cls:
            result = await dispatcher.execute(binding, {})
            mock_cls.assert_not_called()
        assert result.success is False

    @pytest.mark.asyncio
    async def test_non_http_scheme_rejected(self):
        dispatcher = WebhookDispatcher(allowlist={"api.internal"})
        binding = {"url": "file:///etc/passwd"}
        with patch("nthlayer_workers.respond.safe_actions.webhook.httpx.AsyncClient") as mock_cls:
            result = await dispatcher.execute(binding, {})
            mock_cls.assert_not_called()
        assert result.success is False


class TestVariableInjection:
    """Incident-supplied variables containing ``${VAR}`` are rejected."""

    @pytest.mark.asyncio
    async def test_secret_reference_in_variable_rejected(self, monkeypatch):
        monkeypatch.setenv("LEAKED", "secret-value")
        dispatcher = WebhookDispatcher(allowlist={"api.internal"})
        binding = {"url": "https://api.internal/{{target}}", "body": {"t": "{{target}}"}}
        # An attacker-controlled target name like ${LEAKED} must not flow
        # into a secret reference re-resolution.
        result = await dispatcher.execute(binding, {"target": "${LEAKED}"})
        assert result.success is False
        assert "${" in result.detail
        # The resolved secret value must NOT appear in the detail.
        assert "secret-value" not in result.detail

    @pytest.mark.asyncio
    async def test_clean_variable_passes(self):
        dispatcher = WebhookDispatcher(allowlist={"api.internal"})
        binding = {"url": "https://api.internal/{{target}}"}

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.is_success = True
        mock_resp.raise_for_status = MagicMock()

        with patch("nthlayer_workers.respond.safe_actions.webhook.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client
            result = await dispatcher.execute(binding, {"target": "fraud-detect"})
        assert result.success is True


class TestResponseBodyOpacity:
    """Response bodies do not flow into ``detail`` — verdict metadata stays clean."""

    @pytest.mark.asyncio
    async def test_success_detail_is_generic(self):
        dispatcher = WebhookDispatcher(allowlist={"api.internal"})
        binding = {"url": "https://api.internal/action"}

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "REQUEST_ECHO: Authorization: Bearer leaked-secret-123"
        mock_resp.is_success = True
        mock_resp.raise_for_status = MagicMock()

        with patch("nthlayer_workers.respond.safe_actions.webhook.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client
            result = await dispatcher.execute(binding, {})

        assert result.success is True
        assert "leaked-secret-123" not in result.detail
        assert "REQUEST_ECHO" not in result.detail
        assert "200" in result.detail  # status code OK to surface

    @pytest.mark.asyncio
    async def test_error_detail_is_generic(self):
        dispatcher = WebhookDispatcher(allowlist={"api.internal"})
        binding = {"url": "https://api.internal/action"}

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal: SECRET_KEY=leaked"
        mock_resp.is_success = False
        mock_resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError("500", request=MagicMock(), response=mock_resp)
        )

        with patch("nthlayer_workers.respond.safe_actions.webhook.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client
            result = await dispatcher.execute(binding, {})

        assert result.success is False
        assert "SECRET_KEY" not in result.detail
        assert "leaked" not in result.detail


# --- PromQL verification ---

class TestVerification:
    @pytest.mark.asyncio
    async def test_verification_success(self):
        dispatcher = WebhookDispatcher()
        verify_config = {
            "wait": 0,
            "prometheus_url": "http://mock:9090",
            "query": 'up{service="test"} == 1',
            "description": "service is up",
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "data": {"result": [{"value": [1234, "1"]}]}
        }

        with patch("nthlayer_workers.respond.safe_actions.webhook.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            result = await dispatcher._verify(verify_config, {"service": "test"})

        assert result.verified is True
        assert "Verified" in result.verification_detail

    @pytest.mark.asyncio
    async def test_verification_failure(self):
        dispatcher = WebhookDispatcher()
        verify_config = {
            "wait": 0,
            "prometheus_url": "http://mock:9090",
            "query": "error_rate < 0.01",
            "description": "error rate below 1%",
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "data": {"result": [{"value": [1234, "0"]}]}
        }

        with patch("nthlayer_workers.respond.safe_actions.webhook.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            result = await dispatcher._verify(verify_config, {})

        assert result.verified is False

    @pytest.mark.asyncio
    async def test_verification_prometheus_unreachable(self):
        dispatcher = WebhookDispatcher()
        verify_config = {
            "wait": 0,
            "prometheus_url": "http://unreachable:9090",
            "query": "up == 1",
            "description": "check",
        }

        with patch("nthlayer_workers.respond.safe_actions.webhook.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=Exception("Connection refused"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            result = await dispatcher._verify(verify_config, {})

        assert result.verified is None  # unknown, not failure
