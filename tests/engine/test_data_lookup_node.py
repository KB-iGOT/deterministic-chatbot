"""data_lookup node: sync and async service lookups, error routing."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.engine.nodes.data_lookup_node import DataLookupNode
from app.engine.state import initial_state

CFG = {
    "id": "lookup",
    "type": "data_lookup",
    "service": "svc",
    "key": "{{ ctx.collected.k }}",
    "response_mapping": [{"from": "name", "to": "collected.out_name"}],
    "on_success": "ok_node",
    "on_error": {"any": "err_node"},
}


def state(**collected):
    s = initial_state(uuid4(), "u1")
    s.collected = dict(collected)
    return s


class SyncSvc:
    def lookup(self, key):
        return {"name": f"sync:{key}"} if key == "a" else None


class AsyncSvc:
    async def lookup(self, key):
        return {"name": f"async:{key}"}


class BoomSvc:
    def lookup(self, key):
        raise RuntimeError("boom")


def node(svc, cfg=CFG):
    return DataLookupNode(services={"svc": svc} if svc is not None else {}).build(cfg)


class TestRun:
    async def test_sync_service_unchanged(self):
        out = await node(SyncSvc())(state(k="a"))
        assert out["collected"]["out_name"] == "sync:a"
        assert "_last_api_error" not in out["collected"]

    async def test_async_service_is_awaited(self):
        out = await node(AsyncSvc())(state(k="a"))
        assert out["collected"]["out_name"] == "async:a"

    async def test_not_found_records_error(self):
        out = await node(SyncSvc())(state(k="zzz"))
        assert out["collected"]["_last_api_error"] == "not_found"
        assert "out_name" not in out["collected"]

    async def test_missing_service_records_error(self):
        out = await node(None)(state(k="a"))
        assert out["collected"]["_last_api_error"] == "service_not_found"

    async def test_lookup_exception_records_error(self):
        out = await node(BoomSvc())(state(k="a"))
        assert out["collected"]["_last_api_error"].startswith("lookup_error:")

    async def test_previous_error_cleared_on_success(self):
        out = await node(SyncSvc())(state(k="a", _last_api_error="old"))
        assert "_last_api_error" not in out["collected"]


class TestRouting:
    def test_next_node_is_on_success(self):
        assert DataLookupNode(services={}).next_node(CFG) == "ok_node"

    def test_missing_required_field_rejected(self):
        bad = {k: v for k, v in CFG.items() if k != "service"}
        with pytest.raises(ValueError, match="requires 'service'"):
            DataLookupNode(services={}).build(bad)

    def test_conditional_edges_route_on_error(self):
        captured = {}

        class FakeGraph:
            def add_conditional_edges(self, node_id, route, targets):
                captured.update(node_id=node_id, route=route, targets=targets)

        assert DataLookupNode(services={}).register_conditional_edges(FakeGraph(), CFG) is True
        route = captured["route"]
        assert captured["targets"].keys() == {"ok_node", "err_node"}
        assert route(state()) == "ok_node"
        assert route(state(_last_api_error="not_found")) == "err_node"
        assert route(state(_last_api_error="lookup_error:x")) == "err_node"

    def test_no_conditional_edges_without_on_error(self):
        cfg = {k: v for k, v in CFG.items() if k != "on_error"}
        assert DataLookupNode(services={}).register_conditional_edges(object(), cfg) is False
