"""Multi-pod session scenarios through the real routes.

Each "pod" is its own FastAPI app (own in-memory state) sharing one FakeRedis, which is
what separate replicas look like behind a non-sticky load balancer.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes
from app.api.auth import hash_user_id, require_jwt
from app.api.routes import router
from app.services.session_store import SessionStore

PREFIX = "/ai-chatbot/v1"


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    async def setex(self, key, ttl, value):
        self.data[key] = value.encode() if isinstance(value, str) else value

    async def get(self, key):
        return self.data.get(key)

    async def delete(self, key):
        self.data.pop(key, None)

    async def expire(self, key, ttl):
        return key in self.data


class DownRedis:
    async def setex(self, *a):
        raise ConnectionError("redis down")

    get = delete = expire = setex


class StubCompiler:
    def get_categories(self):
        return ["Account"]

    def get_flows_for_category(self, category):
        return [{"flow_id": "f1", "menu_label": "One"}, {"flow_id": "f2", "menu_label": "Two"}]

    def get_menu_items(self):
        return [{"flow_id": "f1", "menu_label": "One"}, {"flow_id": "f2", "menu_label": "Two"}]

    def is_flow_enabled(self, flow_id):
        return True

    def get_flow(self, flow_id):
        return {"flow_id": flow_id, "nodes": []}


class SingleFlowCompiler(StubCompiler):
    def get_flows_for_category(self, category):
        return [{"flow_id": "f1", "menu_label": "One"}]


class StubGraph:
    """Stands in for a compiled LangGraph flow: returns canned results, records calls."""

    def __init__(self, results=None, fail=False, snapshot_values=None):
        self.results = list(results or [])
        self.fail = fail
        self.snapshot_values = snapshot_values or {}
        self.updates: list = []

    async def ainvoke(self, state, config):
        if self.fail:
            raise RuntimeError("flow blew up")
        return self.results.pop(0)

    async def aget_state(self, config):
        return SimpleNamespace(values=self.snapshot_values)

    async def aupdate_state(self, config, update):
        self.updates.append(update)


def _result(status="awaiting_user", node="n1"):
    return {
        "status": status,
        "current_node": node,
        "pending_activities": [{"type": "markdown", "content": f"at {node}"}],
        "collected": {},
    }


def make_pod(redis, compiler=None, graphs=None) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_jwt] = lambda: {"sub": "user-1"}
    app.state.sessions = {}
    app.state.session_store = SessionStore(redis, "ns") if redis is not None else None
    app.state.compiler = compiler or StubCompiler()
    app.state.graphs = graphs if graphs is not None else {}
    app.state.system_messages = {}
    app.state.services = {}
    return TestClient(app)


def create(pod: TestClient) -> str:
    r = pod.post(f"{PREFIX}/sessions/create", json={"channel": "web", "language": "en"})
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


def turn(pod: TestClient, sid: str, choice_id: str):
    return pod.post(f"{PREFIX}/sessions/turn/{sid}", json={"action": "select_choice", "choice_id": choice_id})


def meta(redis: FakeRedis, sid: str) -> dict:
    return json.loads(redis.data[f"ns:session:{sid}"])


def test_turn_on_other_pod_works_and_state_is_shared():
    redis = FakeRedis()
    pod_a, pod_b = make_pod(redis), make_pod(redis)

    sid = create(pod_a)
    r = turn(pod_b, sid, "__cat__Account")  # previously: 404 "session expired"

    assert r.status_code == 200, r.text
    labels = [c["label"] for a in r.json()["activities"] for c in a.get("choices", [])]
    assert labels == ["One", "Two"]
    assert meta(redis, sid)["status"] == "selecting_topic"
    assert meta(redis, sid)["selected_category"] == "Account"
    # With Redis healthy, neither pod keeps a local copy.
    assert pod_a.app.state.sessions == {} and pod_b.app.state.sessions == {}


def test_invalid_choice_keeps_step_and_session_stays_alive_on_any_pod():
    redis = FakeRedis()
    pod_a, pod_b = make_pod(redis), make_pod(redis)
    sid = create(pod_a)

    assert turn(pod_b, sid, "garbage").status_code == 200
    assert meta(redis, sid)["status"] == "selecting_category"
    assert turn(pod_a, sid, "__cat__Account").status_code == 200
    assert meta(redis, sid)["status"] == "selecting_topic"


def test_sessions_list_and_history_work_from_another_pod():
    redis = FakeRedis()
    pod_a, pod_b = make_pod(redis), make_pod(redis)
    sid = create(pod_a)
    turn(pod_a, sid, "__cat__Account")

    r = pod_b.get(f"{PREFIX}/sessions/list")
    assert r.status_code == 200
    assert r.json()["session_id"] == sid
    assert r.json()["status"] == "selecting_topic"  # was None before (metadata was per pod)

    h = pod_b.get(f"{PREFIX}/sessions/history/{sid}")
    assert h.status_code == 200
    assert len(h.json()["messages"]) == 1  # greeting + menu, not an empty list


def test_unknown_session_is_404():
    pod = make_pod(FakeRedis())
    assert turn(pod, "00000000-0000-0000-0000-000000000000", "__cat__Account").status_code == 404


def test_expired_session_is_404_and_removed_from_redis():
    redis = FakeRedis()
    pod_a, pod_b = make_pod(redis), make_pod(redis)
    sid = create(pod_a)

    m = meta(redis, sid)
    m["expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    redis.data[f"ns:session:{sid}"] = json.dumps(m).encode()

    assert turn(pod_b, sid, "__cat__Account").status_code == 404
    assert f"ns:session:{sid}" not in redis.data


def test_redis_down_same_pod_falls_back_to_local_memory():
    pod_a, pod_b = make_pod(DownRedis()), make_pod(DownRedis())
    sid = create(pod_a)

    assert sid in pod_a.app.state.sessions
    assert turn(pod_a, sid, "__cat__Account").status_code == 200
    assert pod_a.app.state.sessions[sid]["status"] == "selecting_topic"
    # Expected limitation: with Redis down another pod cannot see it.
    assert turn(pod_b, sid, "__cat__Account").status_code == 404


def test_baseline_without_shared_store_other_pod_gets_404():
    """The original bug: per-pod memory only, so a turn on another pod is 'expired'."""
    pod_a, pod_b = make_pod(None), make_pod(None)
    sid = create(pod_a)
    assert turn(pod_b, sid, "__cat__Account").status_code == 404


def test_no_redis_configured_behaves_like_before():
    pod = make_pod(None)
    sid = create(pod)
    assert turn(pod, sid, "__cat__Account").status_code == 200
    assert pod.app.state.sessions[sid]["status"] == "selecting_topic"


def test_redis_recovers_and_takes_over_from_local_fallback():
    redis = FakeRedis()
    pod = make_pod(DownRedis())
    sid = create(pod)  # saved locally because Redis is down
    assert sid in pod.app.state.sessions

    pod.app.state.session_store = SessionStore(redis, "ns")  # Redis is back
    assert turn(pod, sid, "__cat__Account").status_code == 200  # local copy still found

    assert meta(redis, sid)["status"] == "selecting_topic"  # now in Redis
    assert sid not in pod.app.state.sessions  # local copy dropped


# ── Flow phases across pods ──────────────────────────────────────────────────

def _flow_pods(redis, graph):
    graphs = {"f1": graph}
    return make_pod(redis, graphs=graphs), make_pod(redis, graphs=graphs)


def test_flow_start_and_turns_alternate_between_pods_and_state_accumulates():
    redis = FakeRedis()
    graph = StubGraph(results=[_result(node="n1"), _result(node="n2"), _result("satisfied", "end")])
    pod_a, pod_b = _flow_pods(redis, graph)

    sid = create(pod_a)
    assert turn(pod_a, sid, "__cat__Account").status_code == 200
    started = turn(pod_b, sid, "f1")  # topic chosen on a different pod than category
    assert started.status_code == 200, started.text
    assert started.json()["flow_id"] == "f1"
    m = meta(redis, sid)
    assert (m["status"], m["flow_id"], m["turn_count"], m["node_path"]) == ("in_flow", "f1", 1, ["n1"])

    r = turn(pod_a, sid, "anything")  # in-flow turn on the other pod
    assert r.status_code == 200 and r.json()["status"] == "awaiting_user"
    m = meta(redis, sid)
    assert (m["turn_count"], m["node_path"]) == (2, ["n1", "n2"])

    r = turn(pod_b, sid, "anything")  # terminal outcome
    assert r.status_code == 200 and r.json()["status"] == "satisfied"
    assert meta(redis, sid)["status"] == "done"

    ended = turn(pod_a, sid, "anything")  # finished session, any pod
    assert ended.status_code == 200 and ended.json()["status"] == "ended"
    assert pod_a.app.state.sessions == {} and pod_b.app.state.sessions == {}
    assert len(graph.updates) >= 3  # history appended each turn


def test_flow_start_error_returns_500_but_session_is_still_saved():
    redis = FakeRedis()
    pod_a, pod_b = _flow_pods(redis, StubGraph(fail=True))
    sid = create(pod_a)
    turn(pod_a, sid, "__cat__Account")

    r = turn(pod_b, sid, "f1")

    assert r.status_code == 500
    assert meta(redis, sid)["status"] == "selecting_topic"  # user can retry on any pod


def test_unknown_topic_reoffers_menu_and_keeps_step():
    redis = FakeRedis()
    pod_a, pod_b = _flow_pods(redis, StubGraph(results=[_result()]))
    sid = create(pod_a)
    turn(pod_a, sid, "__cat__Account")

    r = turn(pod_b, sid, "not-a-flow")

    assert r.status_code == 200 and r.json()["flow_id"] is None
    assert meta(redis, sid)["status"] == "selecting_topic"


def test_single_flow_category_goes_straight_into_the_flow():
    redis = FakeRedis()
    graph = StubGraph(results=[_result(node="n1")])
    graphs = {"f1": graph}
    pod_a = make_pod(redis, compiler=SingleFlowCompiler(), graphs=graphs)
    pod_b = make_pod(redis, compiler=SingleFlowCompiler(), graphs=graphs)
    sid = create(pod_a)

    r = turn(pod_b, sid, "__cat__Account")

    assert r.status_code == 200 and r.json()["flow_id"] == "f1"
    assert meta(redis, sid)["status"] == "in_flow"


def test_in_flow_without_flow_id_returns_500():
    redis = FakeRedis()
    pod = make_pod(redis)
    sid = create(pod)
    m = meta(redis, sid)
    m["status"] = "in_flow"
    redis.data[f"ns:session:{sid}"] = json.dumps(m).encode()

    assert turn(pod, sid, "x").status_code == 500


def test_session_is_saved_even_when_the_turn_raises(monkeypatch):
    redis = FakeRedis()
    pod = make_pod(redis)
    sid = create(pod)

    async def boom(session_id, sid_, session, body, request, claims):
        session["status"] = "selecting_topic"  # mutation made before the failure
        raise RuntimeError("boom")

    monkeypatch.setattr(routes, "_process_turn", boom)
    with pytest.raises(RuntimeError):
        turn(pod, sid, "x")

    assert meta(redis, sid)["status"] == "selecting_topic"


def test_history_from_another_pod_uses_flow_id_from_redis():
    redis = FakeRedis()
    graph = StubGraph(
        results=[_result()],
        snapshot_values={
            "user_id_hash": hash_user_id("user-1"),
            "messages": [{"role": "user", "action": "select_choice", "text": "hi", "ts": "t"}],
        },
    )
    other = StubGraph(snapshot_values={"messages": []})
    pod_a = make_pod(redis, graphs={"f1": graph})
    # pod_b lists another graph first: without flow_id from Redis it would pick the wrong one.
    pod_b = make_pod(redis, graphs={"other": other, "f1": graph})
    sid = create(pod_a)
    turn(pod_a, sid, "__cat__Account")
    turn(pod_a, sid, "f1")

    h = pod_b.get(f"{PREFIX}/sessions/history/{sid}")

    assert h.status_code == 200
    assert [m["text"] for m in h.json()["messages"]] == ["hi"]


def test_history_of_unknown_session_is_empty():
    pod = make_pod(FakeRedis())
    h = pod.get(f"{PREFIX}/sessions/history/00000000-0000-0000-0000-000000000000")
    assert h.status_code == 200 and h.json()["messages"] == []


def test_sessions_list_without_store_or_active_session_returns_null():
    assert make_pod(None).get(f"{PREFIX}/sessions/list").json()["session_id"] is None
    assert make_pod(FakeRedis()).get(f"{PREFIX}/sessions/list").json()["session_id"] is None
