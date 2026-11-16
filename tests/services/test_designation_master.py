"""Tests for DesignationMasterService (Excel parse, search, refresh behaviour)."""

from __future__ import annotations

import asyncio
from io import BytesIO

import pytest
from openpyxl import Workbook

from app.services import designation_master as dm
from app.services.designation_master import (
    DesignationMasterService,
    DesignationMasterUnavailable,
    parse_designations,
)


def make_xlsx(master_rows: list, header: str = "Designation", sheet: str = "Master") -> bytes:
    wb = Workbook()
    wb.active.title = "Your Workspace"
    wb.active.append([header])
    ws = wb.create_sheet(sheet)
    ws.append([header])
    for r in master_rows:
        ws.append([r])
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


ROWS = [
    "Deputy Adviser",
    "Dental Hygenist",
    "Deputy Accountant General",
    "Dental Technician",
    "Assistant Deputy Director",
]


class TestParse:
    def test_reads_master_sheet_sorted(self):
        names = parse_designations(make_xlsx(ROWS))
        assert names == sorted(ROWS, key=str.lower)

    def test_ignores_other_sheets(self):
        # "Your Workspace" only has the header; Master has the data
        assert len(parse_designations(make_xlsx(ROWS))) == len(ROWS)

    def test_skips_blanks_and_dedupes_case_insensitively(self):
        data = make_xlsx(["Clerk", None, "  ", "clerk", "  Senior   Clerk "])
        assert parse_designations(data) == ["Clerk", "Senior Clerk"]

    def test_header_matched_case_insensitively(self):
        assert parse_designations(make_xlsx(["Clerk"], header="DESIGNATION")) == ["Clerk"]

    def test_missing_sheet_raises(self):
        with pytest.raises(ValueError, match="Sheet 'Master' not found"):
            parse_designations(make_xlsx(ROWS, sheet="Other"))

    def test_missing_header_raises(self):
        with pytest.raises(ValueError, match="Designation"):
            parse_designations(make_xlsx(ROWS, header="Name"))


def service_with(monkeypatch, payloads: list):
    """Service whose _download returns/raises the given items in order (last repeats)."""
    svc = DesignationMasterService()
    calls = {"n": 0}

    async def fake_download() -> bytes:
        item = payloads[min(calls["n"], len(payloads) - 1)]
        calls["n"] += 1
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(svc, "_download", fake_download)
    return svc, calls


class TestSearch:
    async def test_prefix_ranks_before_substring(self, monkeypatch):
        svc, _ = service_with(monkeypatch, [make_xlsx(ROWS)])
        res = await svc.search("deputy")
        assert [r["designation"] for r in res] == [
            "Deputy Accountant General",
            "Deputy Adviser",
            "Assistant Deputy Director",
        ]

    async def test_result_shape_uses_name_as_id(self, monkeypatch):
        svc, _ = service_with(monkeypatch, [make_xlsx(ROWS)])
        res = await svc.search("dental tech")
        assert res == [{"id": "Dental Technician", "designation": "Dental Technician"}]

    async def test_case_insensitive_and_multi_token(self, monkeypatch):
        svc, _ = service_with(monkeypatch, [make_xlsx(ROWS)])
        res = await svc.search("DIRECTOR deputy")
        assert [r["designation"] for r in res] == ["Assistant Deputy Director"]

    async def test_limit_and_empty_query(self, monkeypatch):
        svc, _ = service_with(monkeypatch, [make_xlsx(ROWS)])
        assert len(await svc.search("de", limit=2)) == 2
        assert await svc.search("   ") == []
        assert await svc.search("zzz") == []


class TestRefresh:
    async def test_downloads_once_within_ttl(self, monkeypatch):
        svc, calls = service_with(monkeypatch, [make_xlsx(ROWS)])
        await asyncio.gather(*[svc.search("dep") for _ in range(10)])
        await svc.get_all()
        assert calls["n"] == 1

    async def test_first_load_failure_raises_unavailable(self, monkeypatch):
        svc, _ = service_with(monkeypatch, [RuntimeError("boom")])
        with pytest.raises(DesignationMasterUnavailable):
            await svc.search("dep")

    async def test_stale_list_served_and_refreshed_in_background(self, monkeypatch):
        svc, calls = service_with(monkeypatch, [make_xlsx(["Old One"]), make_xlsx(["New One"])])
        assert [r["designation"] for r in await svc.search("one")] == ["Old One"]

        svc._loaded_at -= dm._CACHE_TTL_SECONDS + 1  # expire the cache
        # Stale list is served immediately while the refresh runs in the background
        assert [r["designation"] for r in await svc.search("one")] == ["Old One"]
        await svc._bg_task
        assert [r["designation"] for r in await svc.search("one")] == ["New One"]
        assert calls["n"] == 2

    async def test_failed_refresh_keeps_old_list_and_backs_off(self, monkeypatch):
        svc, calls = service_with(monkeypatch, [make_xlsx(["Old One"]), RuntimeError("down")])
        await svc.search("one")
        svc._loaded_at -= dm._CACHE_TTL_SECONDS + 1

        await svc.search("one")
        await svc._bg_task
        assert [r["designation"] for r in await svc.search("one")] == ["Old One"]
        assert calls["n"] == 2

        # Within the backoff window no further download is attempted
        await svc.search("one")
        assert svc._bg_task.done() and calls["n"] == 2

    async def test_empty_excel_is_rejected(self, monkeypatch):
        svc, _ = service_with(monkeypatch, [make_xlsx([])])
        with pytest.raises(DesignationMasterUnavailable):
            await svc.search("x")


class TestDownload:
    async def test_non_xlsx_response_rejected(self, monkeypatch):
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == dm._API_PATH
            assert request.headers["authorization"].startswith("Bearer ")
            assert request.headers["x-authenticated-user-orgid"] == dm._ROOT_ORG_ID
            assert "x-authenticated-user-token" in request.headers
            return httpx.Response(200, content=b"<html>login</html>")

        svc = DesignationMasterService()
        svc._client = httpx.AsyncClient(
            base_url="http://test", transport=httpx.MockTransport(handler)
        )
        with pytest.raises(ValueError, match="not return an .xlsx"):
            await svc._download()
        await svc._client.aclose()

    async def test_xlsx_response_returned(self):
        import httpx

        payload = make_xlsx(ROWS)
        svc = DesignationMasterService()
        svc._client = httpx.AsyncClient(
            base_url="http://test",
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=payload)),
        )
        assert await svc._download() == payload
        await svc._client.aclose()


class TestLifecycleAndInternals:
    def test_is_configured_follows_api_key(self, monkeypatch):
        svc = DesignationMasterService()
        monkeypatch.setattr(dm.settings, "karmayogi_api_key", "")
        assert svc.is_configured is False
        monkeypatch.setattr(dm.settings, "karmayogi_api_key", "k")
        assert svc.is_configured is True

    async def test_count_and_get_all(self, monkeypatch):
        svc, _ = service_with(monkeypatch, [make_xlsx(ROWS)])
        assert svc.count == 0
        assert len(await svc.get_all()) == len(ROWS)
        assert svc.count == len(ROWS)

    async def test_lookup_empty_key_returns_all(self, monkeypatch):
        svc, _ = service_with(monkeypatch, [make_xlsx(ROWS)])
        for key in ("", "   "):
            res = await svc.lookup(key)
            assert [d["designation"] for d in res["designations"]] == sorted(ROWS, key=str.lower)

    async def test_lookup_with_key_filters_without_limit(self, monkeypatch):
        many = [f"Deputy Officer {i}" for i in range(25)] + ["Clerk"]
        svc, _ = service_with(monkeypatch, [make_xlsx(many)])
        res = await svc.lookup("deputy")
        assert len(res["designations"]) == 25  # not capped at the search default of 10
        assert await svc.lookup("zzz") == {"designations": []}

    async def test_background_refresh_skipped_when_fresh(self, monkeypatch):
        svc, calls = service_with(monkeypatch, [make_xlsx(ROWS)])
        await svc.ensure_loaded()
        await svc._background_refresh()  # not stale → no second download
        assert calls["n"] == 1

    async def test_only_one_background_refresh_at_a_time(self, monkeypatch):
        svc, calls = service_with(monkeypatch, [make_xlsx(ROWS)])
        await svc.ensure_loaded()
        svc._loaded_at -= dm._CACHE_TTL_SECONDS + 1
        svc._start_background_refresh()
        first = svc._bg_task
        svc._start_background_refresh()  # already running → ignored
        assert svc._bg_task is first
        await first
        assert calls["n"] == 2

    async def test_aclose_cancels_task_and_closes_client(self, monkeypatch):
        import httpx

        svc = DesignationMasterService()
        svc._client = httpx.AsyncClient(
            base_url="http://test", transport=httpx.MockTransport(lambda r: httpx.Response(200))
        )
        gate = asyncio.Event()
        svc._bg_task = asyncio.create_task(gate.wait())
        await svc.aclose()
        await asyncio.sleep(0)
        assert svc._bg_task.cancelled()
        assert svc._client is None

    async def test_download_creates_client_with_base_url_and_headers(self, monkeypatch):
        import httpx

        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["headers"] = request.headers
            return httpx.Response(200, content=make_xlsx(ROWS))

        real = httpx.AsyncClient
        monkeypatch.setattr(
            dm.httpx, "AsyncClient",
            lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
        )
        monkeypatch.setattr(dm.settings, "karmayogi_portal_base_url", "https://uat.example/")
        monkeypatch.setattr(dm.settings, "karmayogi_api_key", "secret-key")

        svc = DesignationMasterService()
        content = await svc._download()
        assert content.startswith(b"PK")
        assert seen["url"] == "https://uat.example" + dm._API_PATH
        assert seen["headers"]["authorization"] == "Bearer secret-key"
        assert seen["headers"]["x-authenticated-user-orgid"] == dm._ROOT_ORG_ID
        assert seen["headers"]["x-authenticated-user-token"] == "secret-key"
        await svc.aclose()

    async def test_download_http_error_propagates(self):
        import httpx

        svc = DesignationMasterService()
        svc._client = httpx.AsyncClient(
            base_url="http://test",
            transport=httpx.MockTransport(lambda r: httpx.Response(400, json={"error": "x"})),
        )
        with pytest.raises(httpx.HTTPStatusError):
            await svc._download()
        await svc._client.aclose()


def test_registry_registers_designation_master():
    from app.services.registry import ServiceRegistry

    reg = ServiceRegistry.from_env()
    assert isinstance(reg["designation_master"], DesignationMasterService)
