"""YouTube Data API v3 integration adapter — used only by the Event Video
Missing flow (mode_b_event_related_issues.yaml) to fetch actual video duration.

Auth is a `key=` query param from GOOGLE_YOUTUBE_API_KEY (.env), never
exposed to the flow YAML.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings
from app.engine.nodes.api_call_node import IntegrationNotFound

log = logging.getLogger(__name__)


class YouTubeService:
    """Minimal async HTTP gateway for the YouTube Data API v3."""

    def __init__(self) -> None:
        self.base_url = settings.google_youtube_api_base_url.rstrip("/")
        self.api_key = settings.google_youtube_api_key
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=10.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def execute_request(
        self,
        method: str,
        url: str,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        resp = await self._client.request(
            method=method,
            url=url,
            params={**(params or {}), "key": self.api_key},
            json=body,
            headers=headers,
        )
        if resp.status_code == 404:
            raise IntegrationNotFound(f"YouTube {method} {url} → 404")
        resp.raise_for_status()
        return resp.json()
