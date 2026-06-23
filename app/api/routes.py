"""REST endpoints for the web channel (Phase 1).

Channel architecture:
  - All requests go through the WebAdapter (app/adapters/channel/web.py).
  - Translation happens in the engine runner (app/engine/runner.py), not here.
  - The engine always operates in English; the runner translates in/out.

WebSocket upgrade path (Phase 2):
  Add a /ws/ai-chatbot/v1/sessions/{id} endpoint. The runner's AsyncIterator[Activity]
  interface means zero changes to the engine — only the transport layer changes:

    @router.websocket("/ws/sessions/{session_id}")
    async def ws_chat(ws: WebSocket, session_id: UUID, ...):
        await ws.accept()
        async for activity in run_turn(state, raw_action, translation_svc):
            await ws.send_json(activity.model_dump(exclude_none=True))

Endpoints (all under /ai-chatbot/v1):
    POST   /ai-chatbot/v1/sessions                      Start a new session
    POST   /ai-chatbot/v1/sessions/create               Start a new session
    POST   /ai-chatbot/v1/sessions/turn/{id}            Submit a user action; returns activities
    GET    /ai-chatbot/v1/sessions/list                 Return caller's active session_id (Redis-backed)
    GET    /ai-chatbot/v1/sessions/history/{id}         Full conversation history for a session
    GET    /ai-chatbot/v1/admin/sessions/{id}/trace     Admin-only: full conversation trace
    DELETE /ai-chatbot/v1/admin/sessions/{id}           DPDP DSR: hard-delete session
    GET    /health                                      Liveness check (root-level, not versioned)
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status

from langchain_core.messages import HumanMessage

from app.api.auth import hash_user_id, require_jwt
from app.api.schemas import ActiveSessionResponse, HistoryResponse, MessageEntry, StartSessionRequest, StartSessionResponse, TurnRequest, TurnResponse
from app.config import settings
from app.engine.activity import Activity, QuickReply
from app.engine.runner import _translate_activity
from app.engine.state import Channel, FlowStatus, initial_state
from app.services import tracing

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ai-chatbot/v1")

# ── Menu is driven by flow YAML metadata (menu_label / menu_group / menu_order).
# ── No hardcoded topic mapping here — flow_id IS the button choice_id.
# ── To add a flow to the menu:     set metadata.menu_label in its YAML.
# ── To hide a flow from menu only: set metadata.menu_hidden: true  (API still works).
# ── To disable a flow entirely:    set metadata.enabled: false      (blocked at API).

_TERMINAL_STATUSES = {"satisfied", "ticket_raised", "ended", "error"}


# ── Helpers: dynamic menu + system messages ───────────────────────────────────

def _menu_quick_replies(request: Request) -> list[QuickReply]:
    """Build the topic-picker menu from loaded flow metadata. No Python edits needed."""
    compiler = getattr(request.app.state, "compiler", None)
    if compiler is None:
        return []
    return [
        QuickReply(id=item["flow_id"], label=item["menu_label"])
        for item in compiler.get_menu_items()
    ]


def _category_quick_replies(request: Request) -> list[QuickReply]:
    """Build the top-level category menu from flow metadata.

    Button id uses the ``__cat__`` prefix so submit_turn can distinguish a
    category selection from a flow selection without a separate field.
    Category names and ordering are fully driven by YAML (menu_group /
    menu_group_order) — no hardcoded list here.
    """
    compiler = getattr(request.app.state, "compiler", None)
    if compiler is None:
        return []
    return [
        QuickReply(id=f"__cat__{cat}", label=cat)
        for cat in compiler.get_categories()
    ]


def _flows_for_category_quick_replies(request: Request, category: str) -> list[QuickReply]:
    """Build the sub-flow menu for a specific category."""
    compiler = getattr(request.app.state, "compiler", None)
    if compiler is None:
        return []
    return [
        QuickReply(id=item["flow_id"], label=item["menu_label"])
        for item in compiler.get_flows_for_category(category)
    ]


def _sys(request: Request, key: str, default: str) -> str:
    """Look up a system message by key; fall back to the built-in default."""
    msgs = getattr(request.app.state, "system_messages", {}) or {}
    return msgs.get(key) or default


async def _translate_activities(
    activities: list[dict[str, Any]],
    lang: str,
    svc: Any,
) -> list[dict[str, Any]]:
    """Translate a list of raw activity dicts to `lang` using `svc`.

    Returns the original list unchanged when lang == 'en' or svc is None.
    """
    if not activities or lang == "en" or svc is None:
        return activities
    translated = []
    for raw in activities:
        act = Activity(**raw) if isinstance(raw, dict) else raw
        act = await _translate_activity(act, lang, svc)
        translated.append(act.model_dump(exclude_none=True))
    return translated


@router.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/sessions/create", response_model=StartSessionResponse, tags=["chat"])
async def start_session(
    body: StartSessionRequest,
    request: Request,
    claims: dict[str, Any] = Depends(require_jwt),
) -> StartSessionResponse:
    """Begin a new chat session and return the entry activities."""
    user_id = claims["sub"]
    user_id_hash = hash_user_id(user_id)
    session_id = uuid4()

    ttl_minutes = (
        1440 if body.channel == "whatsapp"
        else settings.igot_web_session_ttl_minutes
    )

    # Store session metadata
    request.app.state.sessions[str(session_id)] = {
        "user_id_hash": user_id_hash,
        "channel": body.channel,
        "language": body.language,
        "flow_id": None,
        "status": "selecting_category",
        "selected_category": None,
        "ttl_minutes": ttl_minutes,
        "turn_count": 0,
        "node_path": [],
        # Langfuse trace-chain IDs — updated after every turn so each turn's
        # span becomes a child of the previous one inside the same LF trace.
        "_lf_trace_id": None,
        "_lf_obs_id": None,
    }

    # Register in Redis so any pod/device can look up this session by user ID
    _store = getattr(request.app.state, "session_store", None)
    if _store:
        await _store.register(user_id_hash, str(session_id), ttl_minutes)

    log.info(
        "[activity] event=session_start  session=%s  user=%s  channel=%s  lang=%s  ttl_min=%d",
        session_id, user_id_hash, body.channel, body.language, ttl_minutes,
    )

    # Greeting + category selection — text from system_messages.yaml, categories from flow metadata
    _sid_str = str(session_id)
    with tracing.turn_trace(
        user_id=user_id,
        session_id=_sid_str,
        trace_name="session-start",
        tags=[body.channel, body.language],
        channel=body.channel,
        language=body.language,
    ):
        # Capture IDs right after span creation — used by the next turn to chain
        _lf_tid, _lf_oid = tracing.get_current_span_ids()

        activities = [
            Activity.markdown(
                _sys(request, "greeting",
                     "👋 Hi! I'm the **iGOT Karmayogi** support assistant.\n\nWhat can I help you with today?")
            ).model_dump(exclude_none=True),
            Activity.quick_replies(
                choices=_category_quick_replies(request)
            ).model_dump(exclude_none=True),
        ]

        # Translate greeting to user's preferred language
        translation_svc = getattr(request.app.state, "services", {}).get("translation")
        activities = await _translate_activities(activities, body.language, translation_svc)

        # What the user effectively "sent": a new session open
        _cats = _category_quick_replies(request)
        tracing.set_span_io(
            input={"event": "session_opened", "channel": body.channel, "language": body.language},
            output={
                "bot": "👋 Greeting shown + category menu",
                "category_options": [qr.label for qr in _cats],
            },
        )

    # Persist LF trace chain IDs so subsequent turns link into the same trace
    _sess = request.app.state.sessions[_sid_str]
    _sess["_lf_trace_id"] = _lf_tid
    _sess["_lf_obs_id"] = _lf_oid

    return StartSessionResponse(
        session_id=session_id,
        activities=activities,
        status=FlowStatus.AWAITING_USER.value,
        flow_id=None,
        current_node=None,
    )


@router.post("/sessions/turn/{session_id}", response_model=TurnResponse, tags=["chat"])
async def submit_turn(
    session_id: UUID,
    body: TurnRequest,
    request: Request,
    claims: dict[str, Any] = Depends(require_jwt),
) -> TurnResponse:
    """Process one user action and return the resulting bot activities."""

    sid = str(session_id)
    sessions: dict = request.app.state.sessions
    session = sessions.get(sid)

    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    user_id = claims["sub"]

    # Translation service + session language (used throughout this handler)
    translation_svc = getattr(request.app.state, "services", {}).get("translation")
    lang = session.get("language", "en")

    # ── Inbound translation: user free-text → English ─────────────────────────
    # Only translate send_message actions; choice_ids are internal identifiers.
    if lang != "en" and translation_svc and body.action == "send_message" and body.text:
        english_text = await translation_svc.to_english(body.text, src=lang)
        body = body.model_copy(update={"text": english_text})

    # ── Phase: category selection (top-level menu, before topic/flow selection) ─
    if session["status"] == "selecting_category":
        choice_id = body.choice_id or ""
        compiler = getattr(request.app.state, "compiler", None)

        _cat_prefix = "__cat__"
        category = choice_id[len(_cat_prefix):] if choice_id.startswith(_cat_prefix) else ""
        sub_flows = _flows_for_category_quick_replies(request, category) if category else []

        if not sub_flows:
            # Invalid selection → re-offer categories
            log.info(
                "[activity] event=category_invalid  session=%s  user=%s  choice=%r",
                sid, session["user_id_hash"], choice_id,
            )
            activities = [
                Activity.markdown(
                    _sys(request, "unknown_topic",
                         "🤔 I didn't catch that — please choose one of the options below.")
                ).model_dump(exclude_none=True),
                Activity.quick_replies(
                    choices=_category_quick_replies(request)
                ).model_dump(exclude_none=True),
            ]
            activities = await _translate_activities(activities, lang, translation_svc)
            return TurnResponse(
                session_id=session_id,
                activities=activities,
                status=FlowStatus.AWAITING_USER.value,
                flow_id=None,
                current_node=None,
            )

        # Valid category — show its flows and advance state
        log.info(
            "[activity] event=category_selected  session=%s  user=%s  category=%r",
            sid, session["user_id_hash"], category,
        )
        session["selected_category"] = category
        session["status"] = "selecting_topic"

        _lf_tid_cat: str | None = None
        _lf_oid_cat: str | None = None
        with tracing.turn_trace(
            user_id=user_id,
            session_id=sid,
            trace_name=f"category-selected",
            tags=[session["channel"], session["language"]],
            trace_id=session.get("_lf_trace_id"),
            parent_observation_id=session.get("_lf_obs_id"),
            category=category,
            channel=session["channel"],
        ):
            _lf_tid_cat, _lf_oid_cat = tracing.get_current_span_ids()
            tracing.set_span_io(
                input={"user": f"Selected category: {category}"},
                output={"bot": "Sub-flow menu shown", "flow_options": [qr.label for qr in sub_flows]},
            )

        if _lf_tid_cat:
            session["_lf_trace_id"] = _lf_tid_cat
        if _lf_oid_cat:
            session["_lf_obs_id"] = _lf_oid_cat

        activities = [
            Activity.markdown(
                _sys(request, "select_issue",
                     "Please choose the specific issue you're facing:")
            ).model_dump(exclude_none=True),
            Activity.quick_replies(choices=sub_flows).model_dump(exclude_none=True),
        ]
        activities = await _translate_activities(activities, lang, translation_svc)
        return TurnResponse(
            session_id=session_id,
            activities=activities,
            status=FlowStatus.AWAITING_USER.value,
            flow_id=None,
            current_node=None,
        )

    # ── Phase: topic selection (before any flow is started) ──────────────────
    if session["status"] == "selecting_topic":
        # choice_id IS the flow_id — no separate mapping table needed.
        # The menu buttons are generated from flow YAML metadata (menu_label).
        choice_id = body.choice_id or ""
        graphs: dict = getattr(request.app.state, "graphs", {})
        compiler = getattr(request.app.state, "compiler", None)

        # Accept the choice only if the flow is compiled AND enabled.
        # metadata.enabled: false blocks the flow at the API level regardless
        # of whether it is compiled (useful for WIP / paused flows).
        flow_id = None
        if choice_id in graphs and compiler is not None and compiler.is_flow_enabled(choice_id):
            flow_id = choice_id

        if not flow_id:
            # Unknown topic → re-offer the sub-menu for the already-chosen category
            # (falls back to the full flat menu when category was never set)
            log.info(
                "[activity] event=topic_invalid  session=%s  user=%s  choice=%r",
                sid, session["user_id_hash"], choice_id,
            )
            _selected_cat = session.get("selected_category")
            _fallback_choices = (
                _flows_for_category_quick_replies(request, _selected_cat)
                if _selected_cat
                else _menu_quick_replies(request)
            )
            activities = [
                Activity.markdown(
                    _sys(request, "unknown_topic",
                         "🤔 I didn't catch that — please choose one of the options below.")
                ).model_dump(exclude_none=True),
                Activity.quick_replies(choices=_fallback_choices).model_dump(exclude_none=True),
            ]
            activities = await _translate_activities(activities, lang, translation_svc)
            return TurnResponse(
                session_id=session_id,
                activities=activities,
                status=FlowStatus.AWAITING_USER.value,
                flow_id=None,
                current_node=None,
            )

        # Start the matched flow (graphs dict already resolved above)
        graph = graphs.get(flow_id)
        if graph is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Flow '{flow_id}' not loaded — check server startup logs",
            )

        user_id_hash = session["user_id_hash"]
        log.info(
            "[activity] event=topic_selected  session=%s  user=%s  flow=%s",
            sid, user_id_hash, flow_id,
        )

        state = initial_state(
            session_id=session_id,
            user_id_hash=user_id_hash,
            channel=Channel(session["channel"]),
            language=session["language"],
            session_ttl_minutes=session["ttl_minutes"],
        )
        state_dict = state.model_dump(mode="json")
        state_dict["flow_id"] = flow_id
        # Seed user_id into collected; store raw JWT in _session_token (not in collected —
        # keeps it out of LLM context and YAML templates; accessible only via __SESSION_TOKEN__ sentinel)
        state_dict.setdefault("collected", {})["user_id"] = user_id
        state_dict["session_token"] = request.headers.get(settings.auth_header_name, "")

        # Pre-fetch user profile (email/name/mobile) once at flow start so every
        # flow has it available without per-flow profile fetch nodes.
        _karmayogi = getattr(request.app.state, "services", {}).get("karmayogi")
        if _karmayogi is not None:
            try:
                _profile = await _karmayogi.execute_request(
                    "POST",
                    "/api/private/user/v1/search",
                    body={"request": {"filters": {"userId": user_id_hash}, "limit": 1}},
                )
                _content = (_profile.get("response") or {}).get("content") or []
                if _content:
                    _personal = (_content[0].get("profileDetails") or {}).get("personalDetails") or {}
                    _profile_fields = {
                        "email":      _personal.get("primaryEmail"),
                        "mobile":     _personal.get("mobile"),
                        "first_name": _personal.get("firstname"),
                        "last_name":  _personal.get("surname"),
                    }
                    state_dict["collected"].update(
                        {k: v for k, v in _profile_fields.items() if v is not None}
                    )
            except Exception:  # noqa: BLE001
                pass  # fail-open: flow continues without profile data

        session["turn_count"] = session.get("turn_count", 0) + 1
        lg_config = {"configurable": {"thread_id": sid}}
        _lf_tid_phase1: str | None = None
        _lf_oid_phase1: str | None = None
        try:
            with tracing.turn_trace(
                user_id=user_id,
                session_id=sid,
                trace_name=f"flow-start-{flow_id}",
                tags=[session["channel"], session["language"], flow_id],
                trace_id=session.get("_lf_trace_id"),
                parent_observation_id=session.get("_lf_obs_id"),
                flow_id=flow_id,
                channel=session["channel"],
                action="topic_selected",
                user_choice=flow_id,
                turn_count=session["turn_count"],
            ):
                _lf_tid_phase1, _lf_oid_phase1 = tracing.get_current_span_ids()
                # topic label comes from the menu — same as flow_id for built-in flows
                _topic_label = next(
                    (qr.label for qr in _menu_quick_replies(request) if qr.id == flow_id),
                    flow_id,
                )
                tracing.set_span_io(input={"user": f"Selected topic: {_topic_label}"})
                result = await graph.ainvoke(state_dict, lg_config)
                result_status = result.get("status", "active")
                _bot_reply = _first_bot_text(result.get("pending_activities") or [])
                tracing.set_span_io(
                    input={"user": f"Selected topic: {_topic_label}"},
                    output={"bot": _bot_reply, "next_node": result.get("current_node"), "status": str(result_status)},
                )
        except Exception as exc:  # noqa: BLE001
            log.exception("Flow start error for %s", flow_id)
            raise HTTPException(status_code=500, detail=f"Flow error: {exc}") from exc

        # Update session trace-chain IDs (span just closed, IDs captured above)
        if _lf_tid_phase1:
            session["_lf_trace_id"] = _lf_tid_phase1
        if _lf_oid_phase1:
            session["_lf_obs_id"] = _lf_oid_phase1

        activities = result.get("pending_activities") or []
        activities = await _translate_activities(activities, lang, translation_svc)
        result_status = result.get("status", "active")

        # Track node progression for this session
        _next_node = result.get("current_node")
        if _next_node:
            session.setdefault("node_path", []).append(_next_node)

        session["flow_id"] = flow_id
        session["status"] = "in_flow" if result_status not in _TERMINAL_STATUSES else "done"

        _store = getattr(request.app.state, "session_store", None)
        if _store:
            if result_status in _TERMINAL_STATUSES:
                await _store.delete(session["user_id_hash"])
            else:
                await _store.refresh(session["user_id_hash"], session["ttl_minutes"])

        if result_status in _TERMINAL_STATUSES:
            _ticket_id = result.get("zoho_ticket_id")
            log.info(
                "[activity] event=flow_ended  session=%s  user=%s  flow=%s  outcome=%s  ticket=%s",
                sid, user_id_hash, flow_id, result_status, _ticket_id or "-",
            )
            tracing.record_session_end(
                user_id=user_id,
                session_id=sid,
                flow_id=flow_id,
                outcome=result_status,
                ticket_id=_ticket_id,
                turn_count=session.get("turn_count", 0),
                node_path=session.get("node_path", []),
                channel=session.get("channel", "web"),
                language=session.get("language", "en"),
                trace_id=session.get("_lf_trace_id"),
                parent_observation_id=session.get("_lf_obs_id"),
            )

        # Append user action + bot response to persistent conversation history
        _history_graph = request.app.state.graphs.get(flow_id) if flow_id else None
        if _history_graph is not None:
            _user_text = (
                body.user_says or body.text or body.choice_id or body.item_label or body.other_query
            )
            _user_msg = {"role": "user", "action": body.action, "text": _user_text, "ts": datetime.utcnow().isoformat()}
            _bot_msg  = {"role": "bot", "activities": activities, "ts": datetime.utcnow().isoformat()}
            try:
                await _history_graph.aupdate_state(
                    {"configurable": {"thread_id": sid}},
                    {"messages": [_user_msg, _bot_msg]},
                )
            except Exception as _exc:  # noqa: BLE001
                log.warning("[history] failed to append messages for session=%s: %s", sid, _exc)

        return TurnResponse(
            session_id=session_id,
            activities=activities,
            status=result_status,
            flow_id=flow_id,
            current_node=result.get("current_node"),
        )

    # ── Phase: active flow ────────────────────────────────────────────────────
    if session["status"] == "done":
        activities = [
            Activity.markdown(
                _sys(request, "conversation_ended",
                     "This conversation has ended. Please start a new session to continue.")
            ).model_dump(exclude_none=True)
        ]
        activities = await _translate_activities(activities, lang, translation_svc)
        return TurnResponse(
            session_id=session_id,
            activities=activities,
            status="ended",
            flow_id=session.get("flow_id"),
            current_node=None,
        )

    flow_id = session.get("flow_id")
    if not flow_id:
        raise HTTPException(status_code=500, detail="Session has no flow_id")

    graphs = getattr(request.app.state, "graphs", {})
    graph = graphs.get(flow_id)
    if graph is None:
        raise HTTPException(status_code=503, detail=f"Flow '{flow_id}' not loaded")

    lg_config = {"configurable": {"thread_id": sid}}
    compiler = request.app.state.compiler

    # Get current graph state for save_to / field lookups
    try:
        snapshot = await graph.aget_state(lg_config)
        current_state_values: dict = snapshot.values if snapshot else {}
    except Exception:  # noqa: BLE001
        current_state_values = {}

    flow_yaml = compiler.get_flow(flow_id)

    # Validate text input BEFORE touching graph state (uses already-translated English text)
    if body.action == "send_message" and body.text:
        current_node_id = current_state_values.get("current_node")
        node_cfg = _find_node(flow_yaml, current_node_id)
        if node_cfg and node_cfg.get("type") == "collect":
            field_meta = _find_current_field(node_cfg, current_state_values.get("collected") or {})
            if field_meta:
                sys_msgs = getattr(request.app.state, "system_messages", {}) or {}
                err = _validate_field_input(body.text, field_meta, sys_msgs)
                if err:
                    # Return immediately — graph state unchanged, user must re-enter
                    err_activities = [
                        Activity.markdown(err).model_dump(exclude_none=True),
                        Activity.input(
                            input_id=field_meta.get("field", field_meta.get("name", "value")),
                            placeholder=field_meta.get("placeholder", ""),
                        ).model_dump(exclude_none=True),
                    ]
                    err_activities = await _translate_activities(err_activities, lang, translation_svc)
                    return TurnResponse(
                        session_id=session_id,
                        activities=err_activities,
                        status=FlowStatus.AWAITING_USER.value,
                        flow_id=flow_id,
                        current_node=current_node_id,
                    )

    # Log the user action (never log free text — PII; log length only)
    _action_detail = _action_summary(body)
    log.info(
        "[activity] event=user_turn  session=%s  user=%s  flow=%s  node=%s  action=%s  %s",
        sid, session["user_id_hash"], flow_id,
        current_state_values.get("current_node", "-"),
        body.action, _action_detail,
    )

    # Build state update from user action (collected fields + message history)
    update = _build_state_update(body, current_state_values, flow_yaml)

    # Safe label for Langfuse — never log free text (PII); use structured fields only
    _user_choice = (
        body.user_says                                              # frontend label e.g. "Certificate issue"
        or body.item_label                                          # picker selection e.g. "Foundation Course on AI"
        or (body.choice_id if body.action == "select_choice" else None)  # internal id fallback
        or body.action                                              # action type as last resort
    )
    session["turn_count"] = session.get("turn_count", 0) + 1

    _lf_tid_active: str | None = None
    _lf_oid_active: str | None = None
    try:
        with tracing.turn_trace(
            user_id=user_id,
            session_id=sid,
            trace_name=f"turn-{flow_id}",
            tags=[session.get("channel", "web"), session.get("language", "en"), flow_id],
            trace_id=session.get("_lf_trace_id"),
            parent_observation_id=session.get("_lf_obs_id"),
            flow_id=flow_id,
            action=body.action,
            node=current_state_values.get("current_node", ""),
            user_choice=_user_choice,
            turn_count=session["turn_count"],
        ):
            _lf_tid_active, _lf_oid_active = tracing.get_current_span_ids()
            # Build a human-readable user label — never log raw text (PII)
            _current_node = current_state_values.get("current_node", "")
            _user_label = _user_input_label(body, _user_choice, _current_node, flow_yaml)
            tracing.set_span_io(input={"user": _user_label, "node": _current_node})
            await graph.aupdate_state(lg_config, update)
            result = await graph.ainvoke(None, lg_config)
            result_status = result.get("status", "active")
            _bot_reply = _first_bot_text(result.get("pending_activities") or [])
            _out: dict = {
                "bot": _bot_reply,
                "next_node": result.get("current_node"),
                "status": str(result_status),
            }
            if result.get("zoho_ticket_id"):
                _out["ticket_id"] = result["zoho_ticket_id"]
            tracing.set_span_io(input={"user": _user_label, "node": _current_node}, output=_out)
    except Exception as exc:  # noqa: BLE001
        log.exception("Flow resume error for session %s", sid)
        raise HTTPException(status_code=500, detail=f"Flow error: {exc}") from exc

    # Advance trace-chain IDs so the next turn links to THIS turn's span
    if _lf_tid_active:
        session["_lf_trace_id"] = _lf_tid_active
    if _lf_oid_active:
        session["_lf_obs_id"] = _lf_oid_active

    activities = result.get("pending_activities") or []
    activities = await _translate_activities(activities, lang, translation_svc)
    result_status = result.get("status", "active")

    # Track node progression
    _next_node = result.get("current_node")
    if _next_node:
        session.setdefault("node_path", []).append(_next_node)

    _store = getattr(request.app.state, "session_store", None)
    if result_status in _TERMINAL_STATUSES:
        session["status"] = "done"
        _ticket_id = result.get("zoho_ticket_id")
        log.info(
            "[activity] event=flow_ended  session=%s  user=%s  flow=%s  outcome=%s  ticket=%s",
            sid, session["user_id_hash"], flow_id, result_status, _ticket_id or "-",
        )
        tracing.record_session_end(
            user_id=user_id,
            session_id=sid,
            flow_id=flow_id,
            outcome=result_status,
            ticket_id=_ticket_id,
            turn_count=session.get("turn_count", 0),
            node_path=session.get("node_path", []),
            channel=session.get("channel", "web"),
            language=session.get("language", "en"),
            trace_id=session.get("_lf_trace_id"),
            parent_observation_id=session.get("_lf_obs_id"),
        )
        if _store:
            await _store.delete(session["user_id_hash"])
    else:
        if _store:
            await _store.refresh(session["user_id_hash"], session["ttl_minutes"])

    # Append user action + bot response to persistent conversation history
    _history_graph = request.app.state.graphs.get(flow_id) if flow_id else None
    if _history_graph is not None:
        _user_text = (
            body.user_says or body.text or body.choice_id or body.item_label or body.other_query
        )
        _user_msg = {"role": "user", "action": body.action, "text": _user_text, "ts": datetime.utcnow().isoformat()}
        _bot_msg  = {"role": "bot", "activities": activities, "ts": datetime.utcnow().isoformat()}
        try:
            await _history_graph.aupdate_state(
                {"configurable": {"thread_id": sid}},
                {"messages": [_user_msg, _bot_msg]},
            )
        except Exception as _exc:  # noqa: BLE001
            log.warning("[history] failed to append messages for session=%s: %s", sid, _exc)

    return TurnResponse(
        session_id=session_id,
        activities=activities,
        status=result_status,
        flow_id=flow_id,
        current_node=result.get("current_node"),
    )


@router.get("/sessions/list", response_model=ActiveSessionResponse, tags=["chat"])
async def get_my_session(
    request: Request,
    claims: dict[str, Any] = Depends(require_jwt),
) -> ActiveSessionResponse:
    """Return the caller's active session ID so the client can resume it.

    Returns session_id=null when:
      - No active session exists for this user
      - The session has expired (TTL elapsed since last turn)
      - Redis is unavailable (fail-open — client should start a new session)

    Client flow:
      1. Call this endpoint on app open.
      2. If session_id is returned  → call GET /sessions/history/{id} to resume.
      3. If null (or history empty) → call POST /sessions/create to start fresh.
    """
    user_id = claims["sub"]
    user_id_hash = hash_user_id(user_id)

    _store = getattr(request.app.state, "session_store", None)
    if _store is None:
        log.debug("[sessions/list] session_store unavailable — returning null")
        return ActiveSessionResponse()

    session_id = await _store.get_active(user_id_hash)
    if not session_id:
        return ActiveSessionResponse()

    # Return any in-memory metadata we have alongside the session_id
    meta = request.app.state.sessions.get(session_id, {})
    return ActiveSessionResponse(
        session_id=session_id,
        status=meta.get("status"),
        flow_id=meta.get("flow_id"),
    )



async def _history_initial_or_empty(
    request: Request,
    session_id: UUID,
    session_meta: dict | None,
) -> HistoryResponse:
    """Return synthetic initial activities if session is active, else empty messages.

    Called from all early-return paths in get_session_history so the frontend
    always gets the greeting + category menu for sessions that exist but have
    no conversation history yet (user opened popup, no topic selected).
    """
    if not session_meta:
        return HistoryResponse(session_id=session_id, messages=[])
    lang = session_meta.get("language", "en")
    translation_svc = getattr(request.app.state, "services", {}).get("translation")
    initial_activities = [
        Activity.markdown(
            _sys(request, "greeting",
                 "👋 Hi! I'm the **iGOT Karmayogi** support assistant.\n\nWhat can I help you with today?")
        ).model_dump(exclude_none=True),
        Activity.quick_replies(
            choices=_category_quick_replies(request)
        ).model_dump(exclude_none=True),
    ]
    initial_activities = await _translate_activities(initial_activities, lang, translation_svc)
    return HistoryResponse(
        session_id=session_id,
        messages=[MessageEntry(
            role="bot",
            activities=initial_activities,
            ts=datetime.utcnow().isoformat(),
        )],
    )


@router.get("/sessions/history/{session_id}", response_model=HistoryResponse, tags=["chat"])
async def get_session_history(
    session_id: UUID,
    request: Request,
    claims: dict[str, Any] = Depends(require_jwt),
) -> HistoryResponse:
    """Return the full conversation history for a session.

    Each entry is either:
      role=user  — the action the user sent (action, text, ts)
      role=bot   — the activities the bot returned (activities[], ts)

    Entries are in chronological order. The last entry is always a bot message
    showing the current state waiting for user input.

    Returns empty messages[] for sessions started before history tracking was added,
    or when no flow has been selected yet (only topic picker shown so far).
    """
    sid = str(session_id)
    user_id = claims["sub"]
    user_id_hash = hash_user_id(user_id)

    # Locate the right graph — need flow_id from session metadata or checkpointer
    session_meta = request.app.state.sessions.get(sid)
    flow_id = session_meta.get("flow_id") if session_meta else None

    graphs: dict = getattr(request.app.state, "graphs", {})
    graph = graphs.get(flow_id) if flow_id else (next(iter(graphs.values())) if graphs else None)

    if graph is None:
        return await _history_initial_or_empty(request, session_id, session_meta)

    lg_config = {"configurable": {"thread_id": sid}}
    try:
        snapshot = await graph.aget_state(lg_config)
    except Exception as exc:  # noqa: BLE001
        log.warning("[history] aget_state failed for session=%s: %s", sid, exc)
        return await _history_initial_or_empty(request, session_id, session_meta)

    if not snapshot or not snapshot.values:
        return await _history_initial_or_empty(request, session_id, session_meta)

    sv = snapshot.values

    # Security: session must belong to the requesting user
    stored_hash = sv.get("user_id_hash", "")
    if stored_hash and stored_hash != user_id_hash:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Session does not belong to this user")

    raw_messages = sv.get("messages") or []
    # Filter to only dict entries that have a "role" key (history entries),
    # skipping any LangChain BaseMessage objects stored by the engine internals.
    history_entries = [m for m in raw_messages if isinstance(m, dict) and "role" in m]
    entries = [MessageEntry(**m) for m in history_entries]

    if not entries and session_meta:
        return await _history_initial_or_empty(request, session_id, session_meta)

    return HistoryResponse(session_id=session_id, messages=entries)


@router.get("/admin/sessions/{session_id}/trace", tags=["admin"])
async def get_session_trace(session_id: UUID, claims: dict[str, Any] = Depends(require_jwt)) -> dict[str, Any]:
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Trace endpoint not yet wired")


@router.delete("/admin/sessions/{session_id}", tags=["admin"])
async def delete_session(session_id: UUID, claims: dict[str, Any] = Depends(require_jwt)) -> dict[str, str]:
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Deletion not yet wired")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _first_bot_text(activities: list[dict]) -> str:
    """Extract the first markdown/text message from a list of bot activities.

    Used to populate the Langfuse span output so the dashboard shows what the
    bot actually said rather than just an activity count.  Returns a short
    preview (≤200 chars) so it fits in Langfuse's metadata column.
    """
    for act in activities:
        text = act.get("text") or act.get("content") or act.get("message") or ""
        if isinstance(text, str) and text.strip():
            return text.strip()[:200]
    return f"[{len(activities)} activities]"


def _user_input_label(
    body: "TurnRequest",
    user_choice: str | None,
    current_node: str,
    flow: dict | None,
) -> str:
    """Build a human-readable, PII-safe label for what the user just did.

    • Choice actions  → the button label the user clicked
    • Pick-item       → the item label selected from a list
    • Send-message    → "Entered: <field_name>" (never the actual text — PII)
    • Fallback        → the action name
    """
    if body.action == "send_message":
        node_cfg = _find_node(flow, current_node)
        field_name = ""
        if node_cfg:
            for p in (node_cfg.get("prompts") or []):
                if not isinstance(p, dict):
                    continue
                fname = p.get("field", "").removeprefix("collected.")
                # We don't have access to collected here, so return first prompt field
                field_name = p.get("label") or fname
                break
            if not field_name:
                fc = node_cfg.get("field") or {}
                field_name = fc.get("label") or fc.get("name", "").removeprefix("collected.") or current_node
        return f"Entered: {field_name or 'text input'}"

    # For choice / pick_item actions use the resolved label
    if user_choice and user_choice != body.action:
        return f"Selected: {user_choice}"

    return body.action


def _build_state_update(
    body: TurnRequest,
    current_state: dict,
    flow: dict | None,
) -> dict:
    """Convert a TurnRequest into a ConversationState partial update.

    Always clears `pending_activities` (prevents re-sending old activities).
    Applies user choice / input to `collected`.
    """
    collected = dict(current_state.get("collected") or {})
    update: dict[str, Any] = {"pending_activities": []}

    current_node_id = current_state.get("current_node")
    node_cfg = _find_node(flow, current_node_id) if flow else None

    if body.action == "select_choice" and body.choice_id:
        collected["_last_choice_id"] = body.choice_id
        # Apply on_reply.save_to if declared in YAML
        if node_cfg:
            on_reply = node_cfg.get("on_reply")
            if isinstance(on_reply, dict):
                save_to = on_reply.get("save_to", "")
                if save_to:
                    field = save_to.removeprefix("collected.")
                    collected[field] = body.choice_id
        # Record step in conversation trail (before updating collected)
        choice_label = _resolve_choice_label(node_cfg, body.choice_id)
        step_label = _get_step_label(node_cfg)
        steps = list(collected.get("_user_steps") or [])
        steps.append(f"{step_label}: {choice_label}")
        collected["_user_steps"] = steps
        update["collected"] = collected
        update["messages"] = [HumanMessage(content=choice_label)]

    elif body.action == "pick_item" and body.item_id:
        collected["_last_choice_id"] = body.item_id
        if node_cfg and node_cfg.get("type") == "collect":
            field_name = (node_cfg.get("field") or {}).get("name", "").removeprefix("collected.")
            if field_name:
                collected[field_name] = body.item_id
            # Merge extra fields that were stored during picker render
            extras = (collected.get("_picker_item_extras") or {}).get(body.item_id, {})
            if extras:
                collected.update(extras)
        update["collected"] = collected
        update["messages"] = [HumanMessage(content=f"Selected: {body.item_id}")]

    elif body.action == "send_message" and body.text:
        # Capture field_meta BEFORE saving (points to the field being filled now)
        field_meta = _find_current_field(node_cfg, collected) if node_cfg and node_cfg.get("type") == "collect" else None
        if node_cfg and node_cfg.get("type") == "collect":
            # Multi-field: find next unfilled required field
            for p in (node_cfg.get("prompts") or []):
                fname = p["field"].removeprefix("collected.")
                if collected.get(fname) is None:
                    collected[fname] = body.text
                    break
            # Single field
            if not node_cfg.get("prompts"):
                fname = (node_cfg.get("field") or {}).get("name", "").removeprefix("collected.")
                if fname:
                    collected[fname] = body.text
        # Record step in conversation trail
        step_label = _get_step_label(node_cfg, field_meta)
        steps = list(collected.get("_user_steps") or [])
        steps.append(f"{step_label}: {body.text}")
        collected["_user_steps"] = steps
        update["collected"] = collected
        update["messages"] = [HumanMessage(content=body.text)]

    return update


def _resolve_choice_label(node_cfg: dict | None, choice_id: str) -> str:
    """Return the human-readable label for a quick_reply choice_id.

    Falls back to the choice_id itself if not found in the node config.
    Checks both top-level quick_replies and follow_up.quick_replies.
    """
    if not node_cfg:
        return choice_id
    qr_list = (node_cfg.get("quick_replies")
                or (node_cfg.get("follow_up") or {}).get("quick_replies")
                or [])
    for qr in qr_list:
        if isinstance(qr, dict) and qr.get("id") == choice_id:
            return qr.get("label", choice_id)
    return choice_id


def _find_node(flow: dict | None, node_id: str | None) -> dict | None:
    if not flow or not node_id:
        return None
    for node in flow.get("nodes", []):
        if node.get("id") == node_id:
            return node
    return None


def _get_step_label(node_cfg: dict | None, field_meta: dict | None = None) -> str:
    """Short readable label for a conversation trail key-value entry.

    For collect nodes: strips boilerplate from the prompt text ("Please share the ...").
    For choice nodes: strips common node-ID prefixes (ask_, branch_, etc.).
    """
    if field_meta:
        text = (field_meta.get("text") or "").strip().rstrip(":")
        text = re.sub(r'(?i)^(please|kindly)?\s*(share|enter|provide|select)\s*(the|your|a)?\s*', '', text).strip()
        if text:
            return text[:60]
        field = (field_meta.get("field") or field_meta.get("name") or "").removeprefix("collected.")
        return field.replace("_", " ").title()[:60]
    if not node_cfg:
        return "Step"
    node_id = node_cfg.get("id", "step")
    for pfx in ("ask_", "branch_on_", "branch_", "show_", "check_", "init_"):
        if node_id.startswith(pfx):
            node_id = node_id[len(pfx):]
            break
    return node_id.replace("_", " ").title()[:60]


def _find_current_field(node_cfg: dict, collected: dict) -> dict | None:
    """Return the prompt/field descriptor for the first unfilled required field."""
    # Multi-prompt sequential collect
    for p in node_cfg.get("prompts") or []:
        fname = p["field"].removeprefix("collected.")
        if collected.get(fname) is None and not p.get("optional"):
            return p
    # Single-field
    field_cfg = node_cfg.get("field")
    if field_cfg:
        return field_cfg
    return None


def _action_summary(body: "TurnRequest") -> str:
    """Return a short non-PII descriptor of the user action for log lines.

    Free text (send_message, request_other) is reduced to its byte length
    so we never write user-entered content into logs.
    """
    if body.action == "select_choice" and body.choice_id:
        return f"choice={body.choice_id}"
    if body.action == "pick_item" and body.item_id:
        return f"item={body.item_id}"
    if body.action == "send_message" and body.text:
        return f"text_len={len(body.text)}"
    if body.action == "request_other" and body.other_query:
        return f"other_len={len(body.other_query)}"
    return ""


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
_DATE_RE  = re.compile(r"\d{1,2}[\s\-/]\w+[\s\-/]\d{2,4}|\d{4}[\-/]\d{2}[\-/]\d{2}")


def _validate_field_input(
    text: str,
    field_meta: dict,
    sys_msgs: dict | None = None,
) -> str | None:
    """Return an error string if the value is invalid, else None.

    Detects field type from name conventions (email, date) and explicit
    `type:` key in the field/prompt descriptor.
    Error strings are read from system_messages.yaml when available.
    """
    _m = sys_msgs or {}
    text = text.strip()
    field_name = (field_meta.get("field") or field_meta.get("name") or "").lower()
    field_type = (field_meta.get("type") or "").lower()

    if not text:
        return _m.get("validation_empty", "❌ This field can't be empty — please enter a value.")

    # Email
    if "email" in field_name or field_type == "email":
        if not _EMAIL_RE.match(text):
            return _m.get(
                "validation_email",
                "❌ That doesn't look like a valid email address.\n"
                "Please enter a valid email, e.g. **name@example.com**",
            )

    # Date
    if "date" in field_name or field_type == "date":
        if not _DATE_RE.search(text):
            return _m.get(
                "validation_date",
                "❌ Please enter a recognisable date, e.g. **12 May 2026** or **2026-05-12**",
            )

    return None
