"""Unit tests for the turn pipeline in app/api/routes.py (_process_turn and its phase handlers).

Everything external (graphs, session store, profile service, translation, Langfuse tracing)
is replaced by in-memory fakes, so these tests drive the real routing/bookkeeping code.
"""

from __future__ import annotations

import contextlib
import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from uuid import UUID

import pytest
from fastapi import HTTPException

from app.api import routes
from app.api.schemas import TurnRequest
from app.services import tracing

SID = UUID("12345678-1234-5678-1234-567812345678")
CLAIMS = {"sub": "u1"}
FLOW_YAML = {
    "nodes": [
        {"id": "n1", "type": "collect", "field": {"field": "email", "placeholder": "e-mail"}},
        {"id": "n2", "type": "message"},
        {"id": "n3", "type": "collect"},
    ]
}
COLLECT_STATE = {"current_node": "n1", "collected": {}}
PROFILE = {
    "response": {
        "content": [
            {
                "profileDetails": {
                    "personalDetails": {
                        "primaryEmail": "e@x.com",
                        "mobile": None,
                        "firstname": "A",
                        "surname": "B",
                    }
                }
            }
        ]
    }
}


# ── fakes ─────────────────────────────────────────────────────────────────────


class Graph:
    def __init__(self, result=None, state=None, fail_on=None):
        self.result = result if result is not None else _result()
        self.state = state if state is not None else {}
        self.fail_on = fail_on
        self.invoked_with = None
        self.updates: list[dict] = []

    async def ainvoke(self, payload, config):
        if self.fail_on == "invoke":
            raise RuntimeError("boom")
        self.invoked_with = payload
        return copy.deepcopy(self.result)

    async def aget_state(self, config):
        if self.fail_on == "get":
            raise RuntimeError("no state")
        return NS(values=copy.deepcopy(self.state))

    async def aupdate_state(self, config, update):
        is_history = [m.get("role") for m in update.get("messages", []) if isinstance(m, dict)] == ["user", "bot"]
        if self.fail_on == "history" and is_history:
            raise RuntimeError("history down")
        self.updates.append(update)


class Store:
    def __init__(self):
        self.calls: list[tuple] = []

    async def delete(self, key):
        self.calls.append(("delete", key))

    async def refresh(self, key, ttl):
        self.calls.append(("refresh", key, ttl))


class Karma:
    def __init__(self, response):
        self.response = response

    async def execute_request(self, *args, **kwargs):
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class Compiler:
    def get_menu_items(self):
        return [{"flow_id": "f1", "menu_label": "F1"}, {"flow_id": "f2", "menu_label": "F2"}]

    def get_categories(self):
        return ["C1", "C2"]

    def get_flows_for_category(self, category):
        flows = {
            "C1": [{"flow_id": "f1", "menu_label": "F1"}, {"flow_id": "f2", "menu_label": "F2"}],
            "C2": [{"flow_id": "f1", "menu_label": "F1"}],
        }
        return flows.get(category, [])

    def is_flow_enabled(self, flow_id):
        return flow_id != "disabled"

    def get_flow(self, flow_id):
        return FLOW_YAML


class Translator:
    async def to_english(self, text, src):
        return f"EN:{text}"


def _result(status="active", node="n2", ticket=None):
    return {
        "status": status,
        "current_node": node,
        "pending_activities": [{"type": "markdown", "content": "hi"}],
        "collected": {"ticket_id": ticket} if ticket else {},
    }


def _request(graphs=None, karma=None, store=True, translator=None, msgs=None):
    services = {"translation": translator}
    if karma is not None:
        services["karmayogi"] = karma
    state = NS(
        graphs=graphs if graphs is not None else {},
        compiler=Compiler(),
        services=services,
        session_store=Store() if store else None,
        system_messages=msgs or {},
    )
    return NS(app=NS(state=state), headers={routes.settings.auth_header_name: "jwt-token"})


def _session(status, **extra):
    session = {
        "status": status,
        "user_id_hash": "uh",
        "channel": "web",
        "language": "en",
        "ttl_minutes": 30,
        "turn_count": 0,
    }
    session.update(extra)
    return session


def _choice(choice_id, **extra):
    return TurnRequest(action="select_choice", choice_id=choice_id, **extra)


def _text(text):
    return TurnRequest(action="send_message", text=text)


async def _turn(session, body, request):
    return await routes._process_turn(SID, str(SID), session, body, request, CLAIMS)


def _texts(response):
    return [a.get("content") for a in response.activities]


@pytest.fixture(autouse=True)
def fake_tracing(monkeypatch):
    calls = NS(turn_traces=[], span_io=[], session_ends=[])

    @contextlib.contextmanager
    def turn_trace(**kwargs):
        calls.turn_traces.append(kwargs)
        yield

    monkeypatch.setattr(tracing, "turn_trace", turn_trace)
    monkeypatch.setattr(tracing, "get_current_span_ids", lambda: ("trace-1", "obs-1"))
    monkeypatch.setattr(tracing, "set_span_io", lambda **kw: calls.span_io.append(kw))
    monkeypatch.setattr(tracing, "record_session_end", lambda **kw: calls.session_ends.append(kw))

    async def passthrough(act, lang, svc):
        return act

    monkeypatch.setattr(routes, "_translate_activity", passthrough)
    return calls


# ── small helpers ─────────────────────────────────────────────────────────────


def test_advance_trace_chain_only_sets_present_ids():
    session: dict = {}
    routes._advance_trace_chain(session, None, None)
    assert session == {}
    routes._advance_trace_chain(session, "t", None)
    assert session == {"_lf_trace_id": "t"}
    routes._advance_trace_chain(session, "t2", "o2")
    assert session == {"_lf_trace_id": "t2", "_lf_obs_id": "o2"}


def test_processing_error_response_is_500():
    resp = routes._processing_error_response()
    assert resp.status_code == 500
    assert b"trouble processing" in resp.body


def test_utc_now_iso_is_naive_utc_iso_string():
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    stamp = routes._utc_now_iso()
    after = datetime.now(timezone.utc).replace(tzinfo=None)

    assert "+" not in stamp and not stamp.endswith("Z")  # same format as legacy utcnow().isoformat()
    parsed = datetime.fromisoformat(stamp)
    assert parsed.tzinfo is None
    assert before - timedelta(seconds=1) <= parsed <= after + timedelta(seconds=1)


async def test_append_history_writes_user_and_bot_messages_with_utc_timestamps():
    graph = Graph()
    request = _request(graphs={"f1": graph})
    activities = [{"type": "markdown", "content": "hi"}]

    await routes._append_history(request, "sid", "f1", _choice("c", user_says="Label"), activities)

    user_msg, bot_msg = graph.updates[0]["messages"]
    assert (user_msg["role"], user_msg["text"], user_msg["action"]) == ("user", "Label", "select_choice")
    assert (bot_msg["role"], bot_msg["activities"]) == ("bot", activities)
    for msg in (user_msg, bot_msg):
        assert datetime.fromisoformat(msg["ts"]).tzinfo is None


async def test_append_history_noop_without_flow_or_graph():
    request = _request(graphs={})
    await routes._append_history(request, "sid", None, _text("x"), [])
    await routes._append_history(request, "sid", "missing", _text("x"), [])


# ── inbound translation ───────────────────────────────────────────────────────


async def test_inbound_text_is_translated_to_english():
    graph = Graph(state=COLLECT_STATE)
    session = _session("in_flow", flow_id="f1", language="hi")
    request = _request(graphs={"f1": graph}, translator=Translator())

    await _turn(session, _text("a@b.com"), request)

    update = graph.updates[0]
    assert "EN:a@b.com" in repr(update)


# ── category phase ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("choice", ["", "plain", "__cat__unknown"])
async def test_category_invalid_reoffers_categories(choice):
    session = _session("selecting_category")
    resp = await _turn(session, _choice(choice), _request(msgs={"unknown_topic": "CUSTOM"}))

    assert resp.status == "awaiting_user"
    assert _texts(resp)[0] == "CUSTOM"
    labels = [c["label"] for c in resp.activities[1]["choices"]]
    assert labels == ["C1", "C2"]
    assert session["status"] == "selecting_category"


async def test_category_multi_flow_shows_submenu_and_links_trace(fake_tracing):
    session = _session("selecting_category")
    resp = await _turn(session, _choice("__cat__C1"), _request())

    assert session["status"] == "selecting_topic"
    assert session["selected_category"] == "C1"
    assert session["_lf_trace_id"] == "trace-1" and session["_lf_obs_id"] == "obs-1"
    assert [c["id"] for c in resp.activities[1]["choices"]] == ["f1", "f2"]
    assert fake_tracing.turn_traces[0]["category"] == "C1"


async def test_category_single_flow_falls_through_and_starts_flow():
    session = _session("selecting_category")
    graph = Graph()
    request = _request(graphs={"f1": graph}, store=True)

    resp = await _turn(session, _choice("__cat__C2"), request)

    assert resp.flow_id == "f1"
    assert session["status"] == "in_flow"
    assert session["flow_id"] == "f1"
    assert graph.invoked_with["flow_id"] == "f1"


# ── topic phase ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("extra", "expected_ids"),
    [({}, ["f1", "f2"]), ({"selected_category": "C2"}, ["f1"])],
)
async def test_topic_invalid_reoffers_menu(extra, expected_ids):
    session = _session("selecting_topic", **extra)
    resp = await _turn(session, _choice("nope"), _request(graphs={"f1": Graph()}))

    assert [c["id"] for c in resp.activities[1]["choices"]] == expected_ids
    assert resp.flow_id is None


async def test_topic_disabled_flow_rejected():
    session = _session("selecting_topic")
    resp = await _turn(session, _choice("disabled"), _request(graphs={"disabled": Graph()}))
    assert resp.status == "awaiting_user"


async def test_topic_without_compiler_rejected():
    request = _request(graphs={"f1": Graph()})
    request.app.state.compiler = None
    resp = await _turn(_session("selecting_topic"), _choice("f1"), request)
    assert resp.activities[1]["choices"] == []


async def test_topic_graph_not_loaded_raises_503():
    with pytest.raises(HTTPException) as exc:
        await _turn(_session("selecting_topic"), _choice("f1"), _request(graphs={"f1": None}))
    assert exc.value.status_code == 503


async def test_topic_start_non_terminal(fake_tracing):
    graph = Graph()
    request = _request(graphs={"f1": graph}, karma=Karma(PROFILE))
    session = _session("selecting_topic")

    resp = await _turn(session, _choice("f1", user_says="F1"), request)

    assert resp.status == "active"
    assert resp.current_node == "n2"
    assert resp.ticket_id is None
    assert session["status"] == "in_flow"
    assert session["flow_id"] == "f1"
    assert session["node_path"] == ["n2"]
    assert session["turn_count"] == 1
    assert session["_lf_trace_id"] == "trace-1"
    # initial state seeded with user id, JWT and profile (None values skipped)
    collected = graph.invoked_with["collected"]
    assert collected["user_id"] == "u1"
    assert collected["email"] == "e@x.com" and collected["first_name"] == "A"
    assert "mobile" not in collected
    assert graph.invoked_with["session_token"] == "jwt-token"
    assert request.app.state.session_store.calls == [("refresh", "uh", 30)]
    # history appended: user + bot messages
    assert [m["role"] for m in graph.updates[0]["messages"]] == ["user", "bot"]
    assert graph.updates[0]["messages"][0]["text"] == "F1"
    assert fake_tracing.session_ends == []


async def test_topic_start_terminal_records_end_and_deletes_session(fake_tracing):
    graph = Graph(_result("ticket_raised", node=None, ticket="TK1"))
    request = _request(graphs={"f1": graph})
    session = _session("selecting_topic")

    resp = await _turn(session, _choice("f1"), request)

    assert resp.ticket_id == "TK1"
    assert session["status"] == "done"
    assert request.app.state.session_store.calls == [("delete", "uh")]
    assert fake_tracing.session_ends[0]["outcome"] == "ticket_raised"
    assert fake_tracing.session_ends[0]["ticket_id"] == "TK1"
    assert "node_path" in fake_tracing.session_ends[0]
    assert "node_path" not in session  # no next node → nothing appended


async def test_topic_start_without_session_store():
    request = _request(graphs={"f1": Graph()}, store=False)
    resp = await _turn(_session("selecting_topic"), _choice("f1"), request)
    assert resp.status == "active"


@pytest.mark.parametrize(
    "response",
    [RuntimeError("down"), {}, {"response": {"content": []}}],
    ids=["error", "empty-dict", "no-content"],
)
async def test_topic_start_profile_fetch_is_fail_open(response):
    graph = Graph()
    request = _request(graphs={"f1": graph}, karma=Karma(response))

    resp = await _turn(_session("selecting_topic"), _choice("f1"), request)

    assert resp.status == "active"
    assert "email" not in graph.invoked_with["collected"]


async def test_topic_start_graph_error_returns_500():
    request = _request(graphs={"f1": Graph(fail_on="invoke")})
    resp = await _turn(_session("selecting_topic"), _choice("f1"), request)
    assert resp.status_code == 500


async def test_topic_start_history_failure_is_swallowed():
    request = _request(graphs={"f1": Graph(fail_on="history")})
    resp = await _turn(_session("selecting_topic"), _choice("f1"), request)
    assert resp.status == "active"


async def test_topic_start_translates_activities_for_session_language():
    request = _request(graphs={"f1": Graph()}, translator=Translator())
    resp = await _turn(_session("selecting_topic", language="hi"), _choice("f1"), request)
    assert _texts(resp) == ["hi"]


# ── active-flow phase ─────────────────────────────────────────────────────────


async def test_active_done_session_reports_ended():
    session = _session("done", flow_id="f1")
    resp = await _turn(session, _text("x"), _request())
    assert resp.status == "ended"
    assert resp.flow_id == "f1"


async def test_active_without_flow_id_returns_500():
    resp = await _turn(_session("in_flow"), _text("x"), _request())
    assert resp.status_code == 500


async def test_active_graph_missing_raises_503():
    with pytest.raises(HTTPException) as exc:
        await _turn(_session("in_flow", flow_id="f1"), _text("x"), _request(graphs={}))
    assert exc.value.status_code == 503


async def test_active_invalid_text_returns_validation_error_without_touching_graph():
    graph = Graph(state=COLLECT_STATE)
    session = _session("in_flow", flow_id="f1")
    resp = await _turn(session, _text("not-an-email"), _request(graphs={"f1": graph}))

    assert resp.status == "awaiting_user"
    assert resp.current_node == "n1"
    assert "email" in _texts(resp)[0].lower()
    assert resp.activities[1]["input_id"] == "email"
    assert graph.updates == [] and graph.invoked_with is None
    assert session["turn_count"] == 0


async def test_active_invalid_text_error_is_translated():
    graph = Graph(state=COLLECT_STATE)
    session = _session("in_flow", flow_id="f1", language="hi")
    resp = await _turn(session, _text("bad"), _request(graphs={"f1": graph}, translator=Translator()))
    assert resp.status == "awaiting_user"


@pytest.mark.parametrize(
    ("state", "body"),
    [
        (COLLECT_STATE, _choice("c")),                                    # not send_message
        (COLLECT_STATE, TurnRequest(action="send_message", text="")),     # no text
        ({"current_node": "n2", "collected": {}}, _text("x")),            # not a collect node
        ({"current_node": "n3", "collected": {}}, _text("x")),            # collect node, no field
        ({"current_node": "unknown", "collected": {}}, _text("x")),       # node not found
    ],
)
async def test_active_validation_skipped(state, body):
    graph = Graph(state=state)
    resp = await _turn(_session("in_flow", flow_id="f1"), body, _request(graphs={"f1": graph}))
    assert resp.status == "active"
    assert graph.invoked_with is None  # resumed with None payload
    assert len(graph.updates) == 2     # state update + history


async def test_active_valid_text_resumes_graph_and_refreshes_store(fake_tracing):
    graph = Graph(state=COLLECT_STATE)
    request = _request(graphs={"f1": graph})
    session = _session("in_flow", flow_id="f1")

    resp = await _turn(session, _text("a@b.com"), request)

    assert resp.status == "active" and resp.current_node == "n2"
    assert session["status"] == "in_flow"
    assert session["turn_count"] == 1
    assert session["node_path"] == ["n2"]
    assert request.app.state.session_store.calls == [("refresh", "uh", 30)]
    assert fake_tracing.turn_traces[0]["trace_name"] == "turn-f1"
    assert [m["role"] for m in graph.updates[-1]["messages"]] == ["user", "bot"]


async def test_active_state_lookup_failure_is_tolerated():
    graph = Graph(fail_on="get")
    resp = await _turn(_session("in_flow", flow_id="f1"), _choice("c"), _request(graphs={"f1": graph}))
    assert resp.status == "active"


async def test_active_terminal_with_ticket(fake_tracing):
    graph = Graph(_result("ticket_raised", node=None, ticket="T9"), state=COLLECT_STATE)
    request = _request(graphs={"f1": graph})
    session = _session("in_flow", flow_id="f1", node_path=["a"])

    resp = await _turn(session, _choice("c", user_says="Cert"), request)

    assert resp.ticket_id == "T9"
    assert session["status"] == "done"
    assert request.app.state.session_store.calls == [("delete", "uh")]
    assert fake_tracing.session_ends[0]["node_path"] == ["a"]
    assert any(io.get("output", {}).get("ticket_id") == "T9" for io in fake_tracing.span_io)


async def test_active_terminal_without_session_store():
    graph = Graph(_result("satisfied", node=None))
    request = _request(graphs={"f1": graph}, store=False)
    session = _session("in_flow", flow_id="f1")
    resp = await _turn(session, _choice("c"), request)
    assert resp.status == "satisfied" and resp.ticket_id is None
    assert session["status"] == "done"


async def test_active_non_terminal_without_session_store():
    request = _request(graphs={"f1": Graph()}, store=False)
    resp = await _turn(_session("in_flow", flow_id="f1"), _choice("c"), request)
    assert resp.status == "active"


async def test_active_graph_error_returns_500():
    graph = Graph(fail_on="invoke")
    resp = await _turn(_session("in_flow", flow_id="f1"), _choice("c"), _request(graphs={"f1": graph}))
    assert resp.status_code == 500


async def test_active_history_failure_is_swallowed():
    graph = Graph(fail_on="history")
    resp = await _turn(_session("in_flow", flow_id="f1"), _choice("c"), _request(graphs={"f1": graph}))
    assert resp.status == "active"
