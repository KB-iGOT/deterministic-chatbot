"""BaseChannelAdapter — abstract contract every channel must implement.

Architecture rule (language-at-boundary):
  Translation happens in engine/runner.py, NOT here. By the time
  transform_activities() is called, text fields are already in the user's
  preferred language. This adapter only deals with format transformation.

Channel-specific constraints live here (e.g. WhatsApp's 3-button limit),
not in flow YAML. Flow authors write channel-agnostic activities.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.engine.activity import Activity, PickerItem, UserAction


class BaseChannelAdapter(ABC):
    """Contract every channel adapter must implement."""

    # Maximum number of quick-reply buttons this channel supports.
    # Engine activities that exceed this will be auto-upgraded to a picker
    # by guard_quick_replies() before the activities are returned to the client.
    max_quick_reply_buttons: int = 10  # safe default; override per channel

    @abstractmethod
    async def receive_message(self, raw_payload: dict[str, Any]) -> UserAction:
        """Transform raw channel payload into a UserAction the engine understands.

        For REST channels this is mostly a passthrough.
        For WhatsApp/Voice the raw webhook structure must be normalised here.
        """
        ...

    @abstractmethod
    def transform_activities(
        self,
        activities: list[Activity],
    ) -> list[dict[str, Any]]:
        """Transform engine activities into channel-native wire format.

        Text fields are already translated (if applicable) when this is called.
        The adapter's job is purely structural: map Activity objects to whatever
        JSON/XML/API shape this channel expects.

        Returns:
            List of channel-native dicts to return or push to the client.
        """
        ...

    def guard_quick_replies(self, activity: Activity) -> Activity:
        """Auto-upgrade quick_replies → picker when button count exceeds channel limit.

        WhatsApp hard-limits interactive buttons to 3. This guard ensures a
        flow YAML author never has to know about that constraint — they write
        quick_replies and the channel adapter silently upgrades when needed.

        Example (WhatsApp, max_quick_reply_buttons=3):
            4 choices → returned as picker (list message)
            3 choices → returned as quick_replies (button message)
        """
        if activity.type != "quick_replies":
            return activity

        choices = activity.choices or []
        if len(choices) <= self.max_quick_reply_buttons:
            return activity

        # Upgrade: convert quick replies to a searchable picker
        return Activity.picker(
            picker_id="auto_upgraded_picker",
            items=[PickerItem(id=c.id, label=c.label) for c in choices],
            placeholder="Select an option",
        )
