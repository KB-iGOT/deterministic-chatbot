"""Engine turn runner — single entry point from channel adapters to LangGraph.

Design: the engine exposes an async generator interface so callers are
future-proof for both REST (collect all activities) and WebSocket (stream
activities as they are produced).

  ┌──────────────────────────────────────────────────────────────┐
  │  REST  → collect all:  [a async for a in run_turn(...)]      │
  │  WS    → stream live:  async for a in run_turn(...): ws.send │
  └──────────────────────────────────────────────────────────────┘

Current implementation (Phase 1 scaffold):
  Runs the LangGraph graph and yields from state.pending_activities.
  Functionally identical to returning a list but callers never need to change.

Phase 2 upgrade:
  Replace the graph.ainvoke() call with graph.astream_events() to yield
  Activity objects as each node completes — callers are unaffected.

Turn pipeline (in order):
  1. Session expiry check   — restart gracefully if TTL exceeded
  2. Inbound translation    — user text → English (if language != 'en')
  3. LangGraph invocation   — run the compiled flow graph
  4. Outbound translation   — engine English output → user's preferred language
  5. TTL refresh            — update expires_at (sliding window)
  6. Yield activities       — one at a time to the caller
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, AsyncGenerator

from app.engine.activity import Activity, PickerItem, QuickReply
from app.engine.state import ConversationState

if TYPE_CHECKING:
    from app.adapters.translation import TranslationService

log = logging.getLogger(__name__)

SESSION_EXPIRED_TEXT = (
    "Your session timed out after inactivity. "
    "Let's start fresh — what can I help you with today?"
)


async def run_turn(
    state: ConversationState,
    raw_action: dict,
    translation_svc: "TranslationService | None" = None,
    session_ttl_minutes: int = 30,
) -> AsyncGenerator[Activity, None]:
    """Run one conversation turn and yield resulting Activity objects.

    Args:
        state:               Current ConversationState (loaded from Redis checkpointer).
        raw_action:          Raw TurnRequest dict from the channel adapter.
        translation_svc:     TranslationService for inbound/outbound translation.
                             If None, no translation is attempted.
        session_ttl_minutes: Sliding TTL to apply on each turn. Defaults to 30 min.

    Yields:
        Activity objects in order. Collect with [a async for a in run_turn(...)].
    """
    return _run_turn_generator(state, raw_action, translation_svc, session_ttl_minutes)


async def _run_turn_generator(
    state: ConversationState,
    raw_action: dict,
    translation_svc: "TranslationService | None",
    session_ttl_minutes: int,
) -> AsyncGenerator[Activity, None]:

    # ── 1. Session expiry check ──────────────────────────────────────────────
    if _is_expired(state):
        log.info("Session %s expired — sending restart message", state.session_id)
        yield Activity.text(SESSION_EXPIRED_TEXT)
        # Caller creates a new session; this generator ends here.
        return

    # ── 2. Inbound translation: user text → English ──────────────────────────
    lang = state.language
    if translation_svc and lang != "en":
        inbound_text = raw_action.get("text") or raw_action.get("other_query", "")
        if inbound_text:
            english_text = await translation_svc.to_english(inbound_text, src=lang)
            # Replace in-flight; engine only ever sees English
            raw_action = {**raw_action, "text": english_text}
            if raw_action.get("other_query"):
                raw_action = {**raw_action, "other_query": english_text}

    # ── 3. LangGraph invocation ──────────────────────────────────────────────
    # Phase 1 scaffold — the real invocation looks like:
    #
    #   result_state = await graph.ainvoke(
    #       input={"messages": [HumanMessage(content=raw_action.get("text", ""))], ...},
    #       config={"configurable": {"thread_id": str(state.session_id)}},
    #   )
    #   state = result_state
    #
    # Phase 2 streaming upgrade (callers unchanged):
    #
    #   async for event in graph.astream_events(..., version="v2"):
    #       if event["event"] == "on_custom_event" and event["name"] == "activity":
    #           activity = Activity(**event["data"])
    #           if translation_svc and lang != "en":
    #               activity = await _translate_activity(activity, lang, translation_svc)
    #           yield activity
    #   return   # skip the pending_activities drain below when streaming

    # ── 4. Yield + translate outbound activities ─────────────────────────────
    for raw in state.pending_activities or []:
        activity = Activity(**raw) if isinstance(raw, dict) else raw
        if translation_svc and lang != "en":
            activity = await _translate_activity(activity, lang, translation_svc)
        yield activity

    # ── 5. Refresh sliding TTL (caller must persist the updated state) ────────
    state.expires_at = datetime.now(timezone.utc) + timedelta(minutes=session_ttl_minutes)
    state.last_activity_at = datetime.now(timezone.utc)


# =============================================================================
# Session expiry helpers
# =============================================================================

def _is_expired(state: ConversationState) -> bool:
    """Return True if the session has passed its expiry timestamp."""
    if state.expires_at is None:
        return False
    return datetime.now(timezone.utc) > state.expires_at


def refresh_ttl(state: ConversationState, ttl_minutes: int) -> None:
    """Extend the session's expiry by ttl_minutes from now.

    Call this after persisting state via the LangGraph checkpointer.
    """
    state.expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
    state.last_activity_at = datetime.now(timezone.utc)


# =============================================================================
# Outbound activity translation
# =============================================================================

async def _translate_activity(
    activity: Activity,
    tgt_lang: str,
    svc: "TranslationService",
) -> Activity:
    """Translate all user-visible text fields in an Activity to tgt_lang.

    Non-destructive: returns a new Activity (model_copy) with translated fields.
    All translation calls for a single activity run concurrently (asyncio.gather)
    to minimise latency when there are multiple strings to translate.
    Activity types with no user-visible text (end, typing, trace) are returned as-is.
    """
    if activity.type in ("end", "typing", "trace"):
        return activity

    # Collect all translation tasks so we can run them in parallel
    tasks: list = []           # coroutines
    task_keys: list[str] = []  # which field each task writes to

    if activity.content:
        tasks.append(svc.from_english(activity.content, tgt_lang))
        task_keys.append("content")

    if activity.choices:
        for i in range(len(activity.choices)):
            tasks.append(svc.from_english(activity.choices[i].label, tgt_lang))
            task_keys.append(f"choice:{i}")

    if activity.items:
        for i in range(len(activity.items)):
            tasks.append(svc.from_english(activity.items[i].label, tgt_lang))
            task_keys.append(f"item:{i}")

    if activity.placeholder:
        tasks.append(svc.from_english(activity.placeholder, tgt_lang))
        task_keys.append("placeholder")

    if not tasks:
        return activity

    # Run all translations for this activity in parallel
    results: list = await asyncio.gather(*tasks)

    updated: dict = {}
    choice_labels: dict[int, str] = {}
    item_labels: dict[int, str] = {}

    for key, result in zip(task_keys, results):
        if key == "content":
            updated["content"] = result
        elif key.startswith("choice:"):
            choice_labels[int(key.split(":")[1])] = result
        elif key.startswith("item:"):
            item_labels[int(key.split(":")[1])] = result
        elif key == "placeholder":
            updated["placeholder"] = result

    if choice_labels:
        updated["choices"] = [
            c.model_copy(update={"label": choice_labels[i]})
            for i, c in enumerate(activity.choices)
        ]

    if item_labels:
        updated["items"] = [
            item.model_copy(update={"label": item_labels[i]})
            for i, item in enumerate(activity.items)
        ]

    return activity.model_copy(update=updated) if updated else activity
