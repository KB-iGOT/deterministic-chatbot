"""ConversationState — the single source of truth for an iGOT Deterministic Chatbot session.

LangGraph persists this to Redis after every node execution via the checkpointer.
"""

from __future__ import annotations

import operator
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class Channel(str, Enum):
    WEB = "web"
    MOBILE = "mobile"
    WHATSAPP = "whatsapp"
    VOICE = "voice"


class FlowStatus(str, Enum):
    """High-level status of where the conversation is in its flow."""
    ACTIVE = "active"
    AWAITING_USER = "awaiting_user"
    AWAITING_CALLBACK = "awaiting_callback"  # async op in-flight (cert reissue, OTP, etc.)
    ESCALATING = "escalating"
    TICKET_RAISED = "ticket_raised"
    SATISFIED = "satisfied"
    ENDED = "ended"
    ERROR = "error"


class TicketDraft(BaseModel):
    """LLM-generated or template-generated ticket fields prior to Zoho POST."""
    subject: str
    description: str          # markdown — shown in chat confirmation preview
    category: str
    sub_category: str | None = None
    classification: str = "Query"
    priority: str = "P3"
    severity: str = "Sev 3"
    portal: str = "Learner Portal"
    conversation_trail: str = ""  # HTML <ol> — user's readable selections for Zoho body


class ConversationState(BaseModel):
    """
    Full state for one user session.

    Persisted by LangGraph Redis checkpointer keyed by `thread_id = session_id`.
    """

    # --- Identity ---
    session_id: UUID
    user_id_hash: str = Field(description="HMAC of Karmayogi userId; never the raw user id")
    channel: Channel = Channel.WEB
    language: str = "en"                    # BCP-47 preferred language (set at session start)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None      # sliding TTL; refreshed on every user turn

    # --- Flow control ---
    flow_id: str | None = None
    current_node: str | None = None
    status: FlowStatus = FlowStatus.ACTIVE

    # --- Conversation history (append-only via operator.add) ---
    # Each entry is a plain dict: {"role": "user"|"bot", ...} for history tracking,
    # or a LangChain BaseMessage for LLM transcript. operator.add appends on every update.
    messages: Annotated[list, operator.add] = Field(default_factory=list)

    # --- Structured data captured by `collect` nodes ---
    collected: dict[str, Any] = Field(default_factory=dict)

    # --- Counters (for `increment_and_branch` Mode B retry pattern) ---
    counters: dict[str, int] = Field(default_factory=dict)

    # --- Output for the current turn (drained by API layer) ---
    pending_activities: list[dict[str, Any]] = Field(default_factory=list)

    # --- Ticket (populated when escalating) ---
    ticket_draft: TicketDraft | None = None
    zoho_ticket_id: str | None = None

    # --- LLM usage tracking (cost cap enforcement) ---
    llm_calls_this_session: int = 0

    # --- Auth (not exposed to YAML templates or LLM context) ---
    # Raw Keycloak JWT for flows that need to forward the user token to privileged APIs.
    # Access only via the "__SESSION_TOKEN__" sentinel in YAML header values — never via ctx.
    session_token: str = ""

    # --- Misc ---
    last_activity_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = {
        "use_enum_values": True,
        "arbitrary_types_allowed": True,
    }


def initial_state(
    session_id: UUID,
    user_id_hash: str,
    channel: Channel | str = Channel.WEB,
    language: str = "en",
    session_ttl_minutes: int = 30,
) -> ConversationState:
    """Factory for a fresh session state.

    Args:
        session_ttl_minutes: Sliding TTL for this session. Refreshed on every
            user turn by the runner. Default 30 min suits web. Pass 1440 (24h)
            for WhatsApp where Meta's window is the binding constraint.
    """
    now = datetime.now(timezone.utc)
    return ConversationState(
        session_id=session_id,
        user_id_hash=user_id_hash,
        channel=channel if isinstance(channel, Channel) else Channel(channel),
        language=language,
        created_at=now,
        expires_at=now + timedelta(minutes=session_ttl_minutes),
    )
