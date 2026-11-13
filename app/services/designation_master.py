"""Designation master-data service.

Downloads the designation master Excel from the Karmayogi portal API, parses the
``Master`` sheet and keeps the designation names in process memory.

Design notes:
  - The Excel is never written to disk: the response bytes go straight into
    openpyxl and are discarded after parsing.
  - One download serves every user. After ``DESIGNATION_CACHE_TTL_SECONDS`` the
    list is refreshed in the background while the previous list keeps being
    served (stale-while-revalidate), so no user waits for a re-download.
  - A failed refresh keeps the last good list and retries after a short backoff.
  - Search is done here (case-insensitive prefix / word / substring ranking),
    because the full list is too long for a chat picker.

The Excel has a single ``Designation`` column and no id column, so the
designation name is used as the id.
"""

from __future__ import annotations

import asyncio
import logging
import time
from io import BytesIO
from typing import Any

import httpx

from app.config import settings

log = logging.getLogger(__name__)

# Host and bearer key are the existing KARMAYOGI_PORTAL_BASE_URL / KARMAYOGI_API_KEY
# settings, same as every other Karmayogi call; only the path differs.
_ROOT_ORG_ID = "0133809553768366080"
_API_PATH = f"/api/designation/v1/orgMapping/sample/{_ROOT_ORG_ID}_odcs"
_SHEET_NAME = "Master"
_CACHE_TTL_SECONDS = 3600    # refresh the list after an hour
_DOWNLOAD_TIMEOUT_SECONDS = 60.0
_XLSX_MAGIC = b"PK"          # .xlsx is a zip container
_RETRY_BACKOFF_SECONDS = 60  # wait this long after a failed refresh before retrying


class DesignationMasterUnavailable(RuntimeError):
    """Raised when no designation list has ever been loaded and loading failed."""


def parse_designations(data: bytes, sheet_name: str = "Master") -> list[str]:
    """Parse designation names out of the master Excel bytes.

    Finds the ``Designation`` column by header name (case-insensitive), skips blank
    rows, strips whitespace, drops case-insensitive duplicates (first spelling
    wins) and returns the names sorted case-insensitively.

    Raises ValueError if the sheet or the header is missing.
    """
    from openpyxl import load_workbook

    wb = load_workbook(BytesIO(data), read_only=True, data_only=True)
    try:
        if sheet_name not in wb.sheetnames:
            raise ValueError(
                f"Sheet {sheet_name!r} not found in designation Excel (sheets: {wb.sheetnames})"
            )
        rows = wb[sheet_name].iter_rows(values_only=True)

        col: int | None = None
        for header in rows:
            for i, cell in enumerate(header):
                if cell is not None and str(cell).strip().lower() == "designation":
                    col = i
                    break
            break  # only the first row is the header
        if col is None:
            raise ValueError("No 'Designation' header in the first row of the Excel sheet")

        seen: set[str] = set()
        names: list[str] = []
        for row in rows:
            if col >= len(row) or row[col] is None:
                continue
            name = " ".join(str(row[col]).split())  # trim + collapse inner whitespace
            key = name.lower()
            if not name or key in seen:
                continue
            seen.add(key)
            names.append(name)
    finally:
        wb.close()

    names.sort(key=str.lower)
    return names


class DesignationMasterService:
    """In-memory designation list backed by the master Excel API."""

    def __init__(self) -> None:
        self._sheet = _SHEET_NAME
        self._ttl = _CACHE_TTL_SECONDS

        self._names: list[str] = []
        self._lower: list[str] = []
        self._loaded_at: float = 0.0
        self._retry_after: float = 0.0
        self._lock = asyncio.Lock()
        self._bg_task: asyncio.Task | None = None
        self._client: httpx.AsyncClient | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    @property
    def is_configured(self) -> bool:
        return bool(settings.karmayogi_api_key)

    async def aclose(self) -> None:
        if self._bg_task is not None and not self._bg_task.done():
            self._bg_task.cancel()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── public API ───────────────────────────────────────────────────────────

    async def ensure_loaded(self) -> None:
        """Make sure a list is available; refresh in the background if it is stale.

        Waits only when nothing has been loaded yet (first use after a restart).
        Raises DesignationMasterUnavailable if that first load fails.
        """
        if self._names:
            if self._is_stale() and time.monotonic() >= self._retry_after:
                self._start_background_refresh()
            return
        async with self._lock:
            if self._names:  # another caller loaded it while we waited
                return
            try:
                await self._refresh()
            except Exception as exc:  # noqa: BLE001
                raise DesignationMasterUnavailable(str(exc)) from exc

    async def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Return up to ``limit`` designations matching ``query``.

        Every whitespace-separated token in the query must appear in the name.
        Ranking: name starts with the query, then a word starts with it, then
        any substring match; ties alphabetical.
        """
        await self.ensure_loaded()
        q = " ".join(query.lower().split())
        if not q:
            return []
        tokens = q.split(" ")

        ranked: list[tuple[int, int]] = []  # (rank, index into the sorted list)
        for idx, low in enumerate(self._lower):
            if not all(t in low for t in tokens):
                continue
            if low.startswith(q):
                rank = 0
            elif f" {q}" in low:
                rank = 1
            else:
                rank = 2
            ranked.append((rank, idx))
        ranked.sort()  # list is already alphabetical, so idx keeps ties alphabetical
        return [
            {"id": self._names[i], "designation": self._names[i]}
            for _, i in ranked[:limit]
        ]

    async def lookup(self, key: str = "") -> dict[str, Any]:
        """Adapter for the ``data_lookup`` flow node: all designations as ``{"designations": [...]}``.

        ``key`` is ignored. Raises DesignationMasterUnavailable when nothing could be
        loaded, which the node routes to its ``on_error`` branch.
        """
        return {"designations": await self.get_all()}

    async def get_all(self) -> list[dict[str, Any]]:
        await self.ensure_loaded()
        return [{"id": n, "designation": n} for n in self._names]

    @property
    def count(self) -> int:
        return len(self._names)

    # ── refresh internals ────────────────────────────────────────────────────

    def _is_stale(self) -> bool:
        return time.monotonic() - self._loaded_at >= self._ttl

    def _start_background_refresh(self) -> None:
        if self._bg_task is not None and not self._bg_task.done():
            return
        self._bg_task = asyncio.create_task(self._background_refresh())

    async def _background_refresh(self) -> None:
        async with self._lock:
            if not self._is_stale():
                return
            try:
                await self._refresh()
            except Exception as exc:  # noqa: BLE001
                self._retry_after = time.monotonic() + _RETRY_BACKOFF_SECONDS
                log.warning(
                    "[designation_master] refresh failed (%s: %s) — keeping %d cached designations",
                    type(exc).__name__, exc, len(self._names),
                )

    async def _refresh(self) -> None:
        data = await self._download()
        names = await asyncio.to_thread(parse_designations, data, self._sheet)
        if not names:
            raise ValueError("designation Excel parsed to an empty list")
        self._names = names
        self._lower = [n.lower() for n in names]
        self._loaded_at = time.monotonic()
        self._retry_after = 0.0
        log.info("[designation_master] loaded %d designations", len(names))

    async def _download(self) -> bytes:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=settings.karmayogi_portal_base_url.rstrip("/"),
                timeout=_DOWNLOAD_TIMEOUT_SECONDS,
                follow_redirects=True,
            )
        resp = await self._client.get(
            _API_PATH,
            # The API requires both x-authenticated-* headers (400 without them).
            # It accepts the service API key as the user token.
            headers={
                "Authorization": f"Bearer {settings.karmayogi_api_key}",
                "x-authenticated-user-orgid": _ROOT_ORG_ID,
                "x-authenticated-user-token": settings.karmayogi_api_key,
            },
        )
        resp.raise_for_status()
        if not resp.content.startswith(_XLSX_MAGIC):
            raise ValueError(
                f"designation API did not return an .xlsx file "
                f"(content-type={resp.headers.get('content-type')!r}, {len(resp.content)} bytes)"
            )
        return resp.content
