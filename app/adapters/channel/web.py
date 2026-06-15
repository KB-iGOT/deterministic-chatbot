"""WebAdapter — REST channel for web and mobile app clients.

The web / mobile clients communicate over standard REST:
  POST /chat/sessions/{id}/turn → returns { activities: [...] }

This adapter is intentionally thin:
  - Activities are returned as JSON dicts (no structural transformation needed)
  - Web has no button-count limits, media restrictions, or template requirements
  - The mobile app uses the identical JSON contract

Future middleware hooks (rate limiting, profanity filter, channel-specific
analytics) can be added here without touching the engine or flow YAML.
"""

from __future__ import annotations

from typing import Any

from app.adapters.channel.base import BaseChannelAdapter
from app.engine.activity import Activity, UserAction


class WebAdapter(BaseChannelAdapter):
    """Channel adapter for web browser and mobile app REST clients."""

    max_quick_reply_buttons = 10  # web UI can render many buttons; no hard limit

    async def receive_message(self, raw_payload: dict[str, Any]) -> UserAction:
        """Parse TurnRequest-shaped payload into a UserAction.

        FastAPI already validates and deserialises TurnRequest. This method
        exists for interface completeness and for future middleware hooks.
        """
        return UserAction(
            action=raw_payload.get("action", "send_message"),
            text=raw_payload.get("text"),
            choice_id=raw_payload.get("choice_id"),
            user_says=raw_payload.get("user_says"),
            picker_id=raw_payload.get("picker_id"),
            item_id=raw_payload.get("item_id"),
            item_label=raw_payload.get("item_label"),
            other_query=raw_payload.get("other_query"),
        )

    def transform_activities(
        self,
        activities: list[Activity],
    ) -> list[dict[str, Any]]:
        """Return activities as plain dicts — the web client JSON contract.

        guard_quick_replies() is applied for consistency but is effectively
        a no-op on web (max_quick_reply_buttons=10 is rarely exceeded).
        """
        result = []
        for activity in activities:
            activity = self.guard_quick_replies(activity)
            result.append(activity.model_dump(exclude_none=True))
        return result
