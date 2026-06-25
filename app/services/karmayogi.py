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
from typing import Any

import httpx

from app.config import settings
from app.engine.nodes.api_call_node import IntegrationNotFound

log = logging.getLogger(__name__)


class KarmayogiService:
    """Async HTTP gateway for Karmayogi platform APIs."""

    def __init__(self) -> None:
        self.base_url = settings.karmayogi_portal_base_url.rstrip("/")
        self.api_key = settings.karmayogi_api_key
        self._client: httpx.AsyncClient | None = None

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

        merged_headers = {
            "Authorization": f"Bearer {self.api_key}",
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
            
            # The Content Search API groups assessments under "QuestionSet".
            # Merge them into "content" so that downstream JSONPaths like $.content[*].name
            # can extract metadata regardless of whether the pending resource is a course or assessment.
            if "QuestionSet" in unwrapped and isinstance(unwrapped["QuestionSet"], list):
                if "content" not in unwrapped or not isinstance(unwrapped["content"], list):
                    unwrapped["content"] = []
                unwrapped["content"].extend(unwrapped["QuestionSet"])
                
            unwrapped["_raw"] = data
            return unwrapped
        return data
