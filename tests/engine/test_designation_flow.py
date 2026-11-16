"""Designation update flow now sourced from the designation_master Excel service."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from openpyxl import Workbook

from app.engine.compiler import FlowCompiler
from app.engine.nodes.data_lookup_node import DataLookupNode
from uuid import uuid4

from app.engine.state import initial_state
from app.services.designation_master import DesignationMasterService, DesignationMasterUnavailable

FLOW_PATH = Path(__file__).resolve().parents[2] / "flows" / "mode_b_profile_completion.yaml"


@pytest.fixture(scope="module")
def nodes():
    flow = FlowCompiler(services={}).load_flow(FLOW_PATH)
    return {n["id"]: n for n in flow["nodes"]}


def make_xlsx(rows: list[str]) -> bytes:
    wb = Workbook()
    wb.active.title = "Master"
    wb.active.append(["Designation"])
    for r in rows:
        wb.active.append([r])
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def service(monkeypatch, payload) -> DesignationMasterService:
    svc = DesignationMasterService()

    async def fake_download() -> bytes:
        if isinstance(payload, Exception):
            raise payload
        return payload

    monkeypatch.setattr(svc, "_download", fake_download)
    return svc


class TestFlowYaml:
    def test_fetch_uses_designation_master_not_v8_api(self, nodes):
        node = nodes["designation_fetch_list"]
        assert node["type"] == "data_lookup"
        assert node["service"] == "designation_master"
        assert node["on_success"] == "designation_search_and_pick"
        assert node["on_error"]["any"] == "designation_no_match_options"
        assert {"from": "designations", "to": "collected._designation_list"} in node["response_mapping"]

    def test_picker_unchanged(self, nodes):
        node = nodes["designation_search_and_pick"]
        assert node["dynamic_options"]["context_key"] == "collected._designation_list"
        assert node["dynamic_options"]["response_mapping"]["id_field"] == "id"
        assert node["dynamic_options"]["response_mapping"]["label_field"] == "designation"

    def test_rest_of_flow_unchanged(self, nodes):
        # Only the list source changed; the MDO-import check is as before.
        tos = [m["to"] for m in nodes["check_mdo_import"]["response_mapping"]]
        assert tos == ["collected.framework_term_codes"]
        rule = nodes["branch_mdo_import"]["rules"][0]
        assert rule["if"] == (
            "ctx.collected.designation_selected_id in (ctx.collected.framework_term_codes or [])"
        )
        assert rule["then"] == "designation_update_steps"
        assert nodes["branch_mdo_import"]["default"] == "lookup_mdo_for_desig"

    def test_menu_still_routes_to_fetch(self, nodes):
        assert any(
            "designation_fetch_list" in (n.get("on_reply") or {}).values()
            or "designation_fetch_list" == (n.get("on_reply") or {}).get("retry")
            for n in nodes.values()
        )


class TestDataLookupWithService:
    def _node(self, nodes, svc):
        return DataLookupNode(services={"designation_master": svc}).build(nodes["designation_fetch_list"])

    async def test_fills_designation_list(self, nodes, monkeypatch):
        svc = service(monkeypatch, make_xlsx(["Deputy Adviser", "Clerk"]))
        out = await self._node(nodes, svc)(initial_state(uuid4(), "u1"))
        assert out["collected"]["_designation_list"] == [
            {"id": "Clerk", "designation": "Clerk"},
            {"id": "Deputy Adviser", "designation": "Deputy Adviser"},
        ]
        assert not out["collected"].get("_last_api_error")

    async def test_load_failure_records_error_for_on_error_route(self, nodes, monkeypatch):
        svc = service(monkeypatch, RuntimeError("API down"))
        out = await self._node(nodes, svc)(initial_state(uuid4(), "u1"))
        assert out["collected"]["_last_api_error"].startswith("lookup_error")
        assert "_designation_list" not in out["collected"]

    async def test_lookup_raises_unavailable(self, monkeypatch):
        svc = service(monkeypatch, RuntimeError("API down"))
        with pytest.raises(DesignationMasterUnavailable):
            await svc.lookup()
