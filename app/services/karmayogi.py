"""Karmayogi platform integration adapter.

Thin HTTP gateway. Provides:
  - Base URL (so YAML uses relative paths like `/api/user/private/v1/read/{user_id}`)
  - Auth header injection (static API key, never exposed to YAML)
  - Optional response unwrapping (Karmayogi APIs typically return `{result: {...}}`)
  - Common retry / timeout policy
  - Mapping of HTTP errors to `IntegrationNotFound` / generic exceptions
    so api_call nodes can route via `on_error` blocks.

All API details (method, path, params, body, response mapping) live in YAML,
NOT here. This is the deliberate design choice that makes flows readable by
non-developers.

Refactor target: lift HTTP execution + auth from `legacy/src/services/`. The
domain-specific methods (`get_user`, `get_enrolment_list`, etc.) that were in
the legacy `user_service.py` are NO LONGER NEEDED — flows call those endpoints
directly via the YAML `request:` block.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.config import settings
from app.engine.nodes.api_call_node import IntegrationNotFound

log = logging.getLogger(__name__)

# Endpoints that require a privileged system-admin Keycloak token as
# x-authenticated-user-token instead of the regular API key.
_SYSTEM_TOKEN_URL_PATTERNS: tuple[str, ...] = (
    "/system/settings/",
)


class KarmayogiService:
    """Async HTTP gateway for Karmayogi platform APIs."""

    def __init__(self) -> None:
        self.base_url = settings.karmayogi_portal_base_url.rstrip("/")
        self.api_key = settings.karmayogi_api_key
        self._client: httpx.AsyncClient | None = None
        self._system_token: str | None = None
        self._system_token_expiry: float = 0.0

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=10.0,
                http2=True,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get_system_token(self) -> str:
        """Fetch (and cache) a Keycloak system-admin token.

        Used for privileged endpoints (e.g. cadreConfig) that require a real
        user token rather than the static API key as x-authenticated-user-token.
        Token is cached until 60 s before expiry.
        """
        if self._system_token and time.time() < self._system_token_expiry:
            return self._system_token

        token_path = settings.access_token_api or "/auth/realms/sunbird/protocol/openid-connect/token"
        token_url = self.base_url + token_path
        # Use a fresh one-shot client so long-lived HTTP/2 connection state
        # from the main API client never interferes with token fetches.
        async with httpx.AsyncClient(timeout=10.0) as token_client:
            resp = await token_client.post(
                token_url,
                data={
                    "grant_type": "password",
                    "username": settings.system_admin_user,
                    "password": settings.system_admin_password,
                    "client_id": "android",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        resp.raise_for_status()
        data = resp.json()
        self._system_token = data["access_token"]
        self._system_token_expiry = time.time() + data.get("expires_in", 3600) - 60
        log.info("[karmayogi] system token refreshed, expires_in=%s", data.get("expires_in"))
        return self._system_token

    async def execute_request(
        self,
        method: str,
        url: str,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Execute the HTTP request declared by an api_call node's `request:` block.

        Adds Karmayogi auth header. Unwraps Karmayogi's `{result: {...}}` envelope
        if present (so YAML `from: $.firstName` works instead of `$.result.firstName`).

        Raises:
            IntegrationNotFound: on HTTP 404
            httpx.HTTPError: on other failures (timeout, connection, 5xx)
        """
        client = await self._get_client()

        # Privileged endpoints (e.g. cadreConfig) require a real Keycloak
        # system-admin token; all others use the static API key.
        needs_system_token = any(pattern in url for pattern in _SYSTEM_TOKEN_URL_PATTERNS)
        if needs_system_token:
            try:
                user_token = await self._get_system_token()
            except Exception as exc:
                log.warning("[karmayogi] system token fetch failed, falling back to api_key: %s", exc)
                user_token = self.api_key
        else:
            user_token = self.api_key

        merged_headers = {
            "Authorization": f"Bearer {self.api_key}",
            "x-authenticated-user-token": user_token,
            "Accept": "application/json",
            **(headers or {}),
        }

        resp = await client.request(
            method=method,
            url=url,
            params=params,
            json=body,
            headers=merged_headers,
        )

        if resp.status_code == 404:
            raise IntegrationNotFound(f"Karmayogi {method} {url} → 404")
        if not resp.is_success:
            log.error(
                "Karmayogi API error: %s %s → HTTP %d  body: %s",
                method, url, resp.status_code, resp.text[:500],
            )
        resp.raise_for_status()
        data = resp.json()

        # Unwrap Karmayogi's {result: {...}} envelope if present, so YAML can
        # use `from: $.firstName` directly. The original wrapped response is
        # preserved as data['_raw'] for completeness.
        if isinstance(data, dict) and "result" in data and isinstance(data["result"], dict):
            unwrapped = data["result"]
            unwrapped["_raw"] = data
            return unwrapped
        return data
