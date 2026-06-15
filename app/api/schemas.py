"""Request / response models for the iGOT Deterministic Chatbot REST API.

These are the wire-format types web and mobile clients send/receive.
Authoritative spec lives in `docs/architecture/INTEGRATION_CONTRACT.md`.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel


class StartSessionRequest(BaseModel):
    channel: Literal["web", "mobile", "whatsapp", "voice"] = "web"
    language: str = "en"
    # Optional resume hint — if client has a prior session_id in localStorage
    resume_session_id: UUID | None = None


class TurnRequest(BaseModel):
    """Single action from the user this turn."""

    action: Literal["start", "send_message", "select_choice", "pick_item", "request_other"]

    # send_message
    text: str | None = None

    # select_choice
    choice_id: str | None = None
    user_says: str | None = None

    # pick_item
    picker_id: str | None = None
    item_id: str | None = None
    item_label: str | None = None

    # request_other
    other_query: str | None = None


class ActivityPayload(BaseModel):
    """Generic activity payload — schema is dynamic per `type:`.

    Detailed schemas per activity type are in INTEGRATION_CONTRACT.md.
    """

    type: str
    # Activity fields vary; we pass through whatever the engine emits
    model_config = {"extra": "allow"}


class TurnResponse(BaseModel):
    session_id: UUID
    activities: list[dict[str, Any]]
    status: str  # FlowStatus value
    flow_id: str | None = None
    current_node: str | None = None
    ticket_id: str | None = None


class StartSessionResponse(TurnResponse):
    resumed: bool = False
