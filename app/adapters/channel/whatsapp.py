"""WhatsAppAdapter — Meta Cloud API channel (Phase 3 stub).

This file stubs the WhatsApp adapter so the architecture is complete and the
interface contract is documented. All methods raise NotImplementedError.
Fill in during Phase 3 when WhatsApp is prioritised.

WhatsApp-specific constraints (enforced by this adapter, NOT by flow YAML):
  ┌────────────────────────────────────────────────────────────┐
  │  quick_replies (≤ 3 choices)  → Interactive Button Message │
  │  quick_replies (> 3 choices)  → auto-upgraded to picker    │
  │  picker                       → Interactive List Message   │
  │  text / markdown              → Text message (max 4096 ch) │
  │  typing                       → Ignored (no WA API)        │
  │  end                          → Ignored                    │
  └────────────────────────────────────────────────────────────┘

Meta Cloud API endpoint:
  POST https://graph.facebook.com/v19.0/{phone_number_id}/messages
  Authorization: Bearer {access_token}

Webhook inbound payload shape:
  {
    "entry": [{
      "changes": [{
        "value": {
          "messages": [{ "from": "...", "type": "text", "text": {"body": "..."} }]
        }
      }]
    }]
  }

Session window: Meta's 24h customer-initiated window applies.
  Pass session_ttl_minutes=1440 to initial_state() for WhatsApp sessions.
  On window expiry, only template messages can be sent (handled separately).
"""

from __future__ import annotations

from typing import Any

from app.adapters.channel.base import BaseChannelAdapter
from app.engine.activity import Activity, UserAction


class WhatsAppAdapter(BaseChannelAdapter):
    """Channel adapter for WhatsApp via Meta Cloud API (Phase 3)."""

    max_quick_reply_buttons = 3  # Meta hard limit for interactive button messages

    def __init__(self, phone_number_id: str, access_token: str) -> None:
        self.phone_number_id = phone_number_id
        self.access_token = access_token

    async def receive_message(self, raw_payload: dict[str, Any]) -> UserAction:
        """Parse Meta webhook payload into a UserAction.

        TODO Phase 3: implement full webhook normalisation.
        Handle: text messages, button replies, list replies, interactive actions.
        """
        raise NotImplementedError(
            "WhatsAppAdapter.receive_message — implement in Phase 3. "
            "See module docstring for expected payload shape."
        )

    def transform_activities(
        self,
        activities: list[Activity],
    ) -> list[dict[str, Any]]:
        """Transform engine activities into Meta Cloud API message objects.

        TODO Phase 3: implement full transformation.

        Planned transforms:
          text / markdown  → { type: "text", text: { body: "..." } }
          quick_replies    → { type: "interactive", interactive: { type: "button", ... } }
          picker           → { type: "interactive", interactive: { type: "list", ... } }
          typing           → skipped (no WhatsApp API support)
          end              → skipped
        """
        raise NotImplementedError(
            "WhatsAppAdapter.transform_activities — implement in Phase 3."
        )

    async def send_activities(
        self,
        to_number: str,
        activities: list[dict[str, Any]],
    ) -> None:
        """Push transformed activities to Meta Cloud API.

        TODO Phase 3: HTTP POST to
          https://graph.facebook.com/v19.0/{phone_number_id}/messages
        Each activity is a separate API call (Meta sends one message at a time).
        """
        raise NotImplementedError(
            "WhatsAppAdapter.send_activities — implement in Phase 3."
        )
