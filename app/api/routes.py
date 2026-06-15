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
    POST   /ai-chatbot/v1/sessions                  Start a new session
    POST   /ai-chatbot/v1/sessions/{id}/turn        Submit a user action; returns activities
    GET    /ai-chatbot/v1/sessions/{id}             Resume an existing session
    GET    /ai-chatbot/v1/admin/sessions/{id}/trace Admin-only: full conversation trace
    DELETE /ai-chatbot/v1/admin/sessions/{id}       DPDP DSR: hard-delete session
    GET    /health                                  Liveness check (root-level, not versioned)
"""

from __future__ import annotations

import logging
import re
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status

from langchain_core.messages import HumanMessage

from app.api.auth import hash_user_id, require_jwt
from app.api.schemas import StartSessionRequest, StartSessionResponse, TurnRequest, TurnResponse
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


@router.post("/sessions", response_model=StartSessionResponse, tags=["chat"])
async def start_session(
    body: StartSessionRequest,
    request: Request,
    claims: dict[str, Any] = Depends(require_jwt),
) -> StartSessionResponse:
    """Begin a new chat session and return the entry activities."""
    user_id = claims["sub"]
    user_id_hash = hash_user_id(user_id)
    session_id = body.resume_session_id or uuid4()

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
        "status": "selecting_topic",
        "ttl_minutes": ttl_minutes,
    }

    log.info(
        "[activity] event=session_start  session=%s  user=%s  channel=%s  lang=%s  ttl_min=%d  resumed=%s",
        session_id, user_id_hash, body.channel, body.language, ttl_minutes,
        body.resume_session_id is not None,
    )

    # Greeting + topic selection — text from system_messages.yaml, menu from flow metadata
    with tracing.turn_trace(
        user_id=user_id_hash,
        session_id=str(session_id),
        trace_name="session-start",
        tags=[body.channel, body.language],
        channel=body.channel,
        language=body.language,
        resumed=str(body.resume_session_id is not None),
    ):
        activities = [
            Activity.markdown(
                _sys(request, "greeting",
                     "👋 Hi! I'm the **iGOT Karmayogi** support assistant.\n\nWhat can I help you with today?")
            ).model_dump(exclude_none=True),
            Activity.quick_replies(
                choices=_menu_quick_replies(request)
            ).model_dump(exclude_none=True),
        ]

        # Translate greeting to user's preferred language
        translation_svc = getattr(request.app.state, "services", {}).get("translation")
        activities = await _translate_activities(activities, body.language, translation_svc)

        tracing.set_trace_io(
            input={"channel": body.channel, "language": body.language},
            output={"menu_items": len(_menu_quick_replies(request))},
        )

    return StartSessionResponse(
        session_id=session_id,
        activities=activities,
        status=FlowStatus.AWAITING_USER.value,
        flow_id=None,
        current_node=None,
        resumed=body.resume_session_id is not None,
    )


@router.post("/sessions/{session_id}/turn", response_model=TurnResponse, tags=["chat"])
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

    # Translation service + session language (used throughout this handler)
    translation_svc = getattr(request.app.state, "services", {}).get("translation")
    lang = session.get("language", "en")

    # ── Inbound translation: user free-text → English ─────────────────────────
    # Only translate send_message actions; choice_ids are internal identifiers.
    if lang != "en" and translation_svc and body.action == "send_message" and body.text:
        english_text = await translation_svc.to_english(body.text, src=lang)
        body = body.model_copy(update={"text": english_text})

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
            # Unknown topic → re-offer the menu (re-read from metadata so it's always fresh)
            log.info(
                "[activity] event=topic_invalid  session=%s  user=%s  choice=%r",
                sid, session["user_id_hash"], choice_id,
            )
            activities = [
                Activity.markdown(
                    _sys(request, "unknown_topic",
                         "🤔 I didn't catch that — please choose one of the options below.")
                ).model_dump(exclude_none=True),
                Activity.quick_replies(
                    choices=_menu_quick_replies(request)
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

        lg_config = {"configurable": {"thread_id": sid}}
        try:
            with tracing.turn_trace(
                user_id=user_id_hash,
                session_id=sid,
                trace_name=f"flow-start-{flow_id}",
                tags=[session["channel"], session["language"], flow_id],
                flow_id=flow_id,
                channel=session["channel"],
                action="topic_selected",
            ):
                tracing.set_trace_io(input={"flow_id": flow_id, "action": "topic_selected"})
                result = await graph.ainvoke(state_dict, lg_config)
                result_status = result.get("status", "active")
                tracing.set_trace_io(
                    input={"flow_id": flow_id, "action": "topic_selected"},
                    output={"status": result_status, "node": result.get("current_node")},
                )
        except Exception as exc:  # noqa: BLE001
            log.exception("Flow start error for %s", flow_id)
            raise HTTPException(status_code=500, detail=f"Flow error: {exc}") from exc

        activities = result.get("pending_activities") or []
        activities = await _translate_activities(activities, lang, translation_svc)
        result_status = result.get("status", "active")

        session["flow_id"] = flow_id
        session["status"] = "in_flow" if result_status not in _TERMINAL_STATUSES else "done"

        if result_status in _TERMINAL_STATUSES:
            log.info(
                "[activity] event=flow_ended  session=%s  user=%s  flow=%s  outcome=%s  ticket=%s",
                sid, user_id_hash, flow_id, result_status,
                result.get("zoho_ticket_id") or "-",
            )

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

    try:
        with tracing.turn_trace(
            user_id=session["user_id_hash"],
            session_id=sid,
            trace_name=f"turn-{flow_id}",
            tags=[session.get("channel", "web"), session.get("language", "en"), flow_id],
            flow_id=flow_id,
            action=body.action,
            node=current_state_values.get("current_node", ""),
        ):
            tracing.set_trace_io(
                input={
                    "action": body.action,
                    "node": current_state_values.get("current_node"),
                    **({f"choice_id": body.choice_id} if body.choice_id else {}),
                }
            )
            await graph.aupdate_state(lg_config, update)
            result = await graph.ainvoke(None, lg_config)
            result_status = result.get("status", "active")
            tracing.set_trace_io(
                input={
                    "action": body.action,
                    "node": current_state_values.get("current_node"),
                    **({f"choice_id": body.choice_id} if body.choice_id else {}),
                },
                output={
                    "status": result_status,
                    "node": result.get("current_node"),
                    "activities": len(result.get("pending_activities") or []),
                    **({"ticket_id": result.get("zoho_ticket_id")} if result.get("zoho_ticket_id") else {}),
                },
            )
    except Exception as exc:  # noqa: BLE001
        log.exception("Flow resume error for session %s", sid)
        raise HTTPException(status_code=500, detail=f"Flow error: {exc}") from exc

    activities = result.get("pending_activities") or []
    activities = await _translate_activities(activities, lang, translation_svc)
    result_status = result.get("status", "active")

    if result_status in _TERMINAL_STATUSES:
        session["status"] = "done"
        log.info(
            "[activity] event=flow_ended  session=%s  user=%s  flow=%s  outcome=%s  ticket=%s",
            sid, session["user_id_hash"], flow_id, result_status,
            result.get("zoho_ticket_id") or "-",
        )

    return TurnResponse(
        session_id=session_id,
        activities=activities,
        status=result_status,
        flow_id=flow_id,
        current_node=result.get("current_node"),
    )


@router.get("/sessions/{session_id}", response_model=TurnResponse, tags=["chat"])
async def resume_session(session_id: UUID, claims: dict[str, Any] = Depends(require_jwt)) -> TurnResponse:
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Resume not yet wired")


@router.get("/admin/sessions/{session_id}/trace", tags=["admin"])
async def get_session_trace(session_id: UUID, claims: dict[str, Any] = Depends(require_jwt)) -> dict[str, Any]:
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Trace endpoint not yet wired")


@router.delete("/admin/sessions/{session_id}", tags=["admin"])
async def delete_session(session_id: UUID, claims: dict[str, Any] = Depends(require_jwt)) -> dict[str, str]:
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Deletion not yet wired")


# ── Helpers ───────────────────────────────────────────────────────────────────

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
        update["collected"] = collected
        # Record user choice as a human message so the LLM transcript is populated
        choice_label = _resolve_choice_label(node_cfg, body.choice_id)
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
