"""Zoho Desk integration adapter.

Thin HTTP gateway. Provides:
  - Base URL (so YAML uses relative paths like `/tickets`)
  - OAuth refresh-token grant; access token cached with `expires_in - 5min` buffer
  - 401 → force-refresh + retry up to 3x
  - orgId header injection
  - Mapping of HTTP errors to standard exceptions for on_error routing

All ticket payload details (subject, description, custom fields, etc.) live in
YAML — see `integrations/zoho_ticket_properties_mapping.json` for the field
schema reference.

Refactor source: `legacy/src/services/zoho_utils.py`.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.config import settings
from app.engine.nodes.api_call_node import IntegrationNotFound

log = logging.getLogger(__name__)


class ZohoDeskAdapter:
    """Async HTTP gateway for Zoho Desk."""

    # Class-level token cache — shared across all instances in a process.
    # Prevents duplicate token refreshes when the adapter is re-instantiated
    # (e.g. in tests or worker restarts) before the previous token has expired.
    _cls_access_token: str | None = None
    _cls_expires_at: float = 0.0

    def __init__(self) -> None:
        self.base_url = settings.zoho_base_url.rstrip("/")
        self.oauth_base = settings.zoho_oauth_base.rstrip("/")
        self.refresh_token = settings.zoho_refresh_token
        self.client_id = settings.zoho_client_id
        self.client_secret = settings.zoho_client_secret
        self.org_id = settings.zoho_org_id

        self._client: httpx.AsyncClient | None = None

        if not self.refresh_token:
            log.warning(
                "[zoho] No ZOHO_REFRESH_TOKEN configured — running in STUB mode. "
                "Ticket creation will return a fake token and Zoho API calls will fail."
            )
        else:
            log.info("[zoho] Adapter initialised. base_url=%s org_id=%s", self.base_url, self.org_id)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=10.0)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _ensure_token(self, force_refresh: bool = False) -> str:
        now = time.time()
        # Use class-level cache — valid across all instances for the process lifetime
        if not force_refresh and ZohoDeskAdapter._cls_access_token and now < ZohoDeskAdapter._cls_expires_at - 300:
            log.debug(
                "[zoho] Reusing cached access token (expires in %.0fs)",
                ZohoDeskAdapter._cls_expires_at - now,
            )
            return ZohoDeskAdapter._cls_access_token

        if not self.refresh_token:
            log.warning("[zoho] Stub mode — returning placeholder token (no real Zoho creds)")
            ZohoDeskAdapter._cls_access_token = "<stub-zoho-token>"
            ZohoDeskAdapter._cls_expires_at = now + 3600
            return ZohoDeskAdapter._cls_access_token

        log.info("[zoho] Refreshing OAuth access token (force=%s)", force_refresh)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{self.oauth_base}/oauth/v2/token",
                    data={
                        "refresh_token": self.refresh_token,
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "grant_type": "refresh_token",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            log.error(
                "[zoho] Token refresh HTTP error: status=%d body=%s",
                exc.response.status_code,
                exc.response.text[:500],
            )
            raise
        except Exception as exc:  # noqa: BLE001
            log.error("[zoho] Token refresh failed: %s", exc)
            raise

        ZohoDeskAdapter._cls_access_token = data["access_token"]
        expires_in = int(data.get("expires_in", 3600))
        ZohoDeskAdapter._cls_expires_at = now + expires_in
        log.info("[zoho] Token refreshed successfully. expires_in=%ds", expires_in)
        return ZohoDeskAdapter._cls_access_token

    async def execute_request(
        self,
        method: str,
        url: str,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Execute an HTTP request against Zoho Desk with OAuth + retry."""
        client = await self._get_client()

        for attempt in (1, 2, 3):
            log.debug("[zoho] %s %s (attempt %d/3) params=%s body=%s", method, url, attempt, params, str(body)[:800] if body else "—")
            token = await self._ensure_token(force_refresh=(attempt > 1))
            merged_headers = {
                "Authorization": f"Zoho-oauthtoken {token}",
                "orgId": self.org_id,
                "Accept": "application/json",
                **(headers or {}),
            }

            try:
                resp = await client.request(
                    method=method,
                    url=url,
                    params=params,
                    json=body,
                    headers=merged_headers,
                )
            except httpx.RequestError as exc:
                log.error("[zoho] Network error on attempt %d for %s %s: %s", attempt, method, url, exc)
                if attempt == 3:
                    raise
                continue

            if resp.status_code == 404:
                log.warning("[zoho] 404 Not Found: %s %s", method, url)
                raise IntegrationNotFound(f"Zoho {method} {url} → 404")

            if resp.status_code == 401:
                log.warning(
                    "[zoho] 401 Unauthorized on attempt %d for %s %s — forcing token refresh",
                    attempt, method, url,
                )
                if attempt < 3:
                    continue
                log.error("[zoho] 401 persisted after %d attempts — giving up", attempt)
                resp.raise_for_status()

            if not resp.is_success:
                log.error(
                    "[zoho] HTTP %d for %s %s\n  request body: %s\n  response: %s",
                    resp.status_code,
                    method,
                    url,
                    str(body)[:500] if body else "—",
                    resp.text[:500],
                )
                resp.raise_for_status()

            log.info("[zoho] %s %s → %d OK", method, url, resp.status_code)
            return resp.json()

        log.error("[zoho] All 3 retry attempts exhausted for %s %s", method, url)
        raise httpx.RequestError("Zoho retries exhausted")
