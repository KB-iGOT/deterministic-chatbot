"""Activity envelope — channel-agnostic structured bot output.

Each "turn" the engine emits a list of Activity objects. The channel adapter
renders them in its native idiom (HTML buttons for web, Interactive Messages
for WhatsApp, TTS for voice).

See `docs/architecture/INTEGRATION_CONTRACT.md` (when written) for the
authoritative protocol spec for web/mobile developers.
"""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, Field


class QuickReply(BaseModel):
    """A single button in a quick_replies activity."""
    id: str
    label: str
    spoken_label: str | None = None   # Voice/IVR: how TTS reads it
    dtmf: str | None = None           # IVR: keypad mapping ("1".."9")
    icon: str | None = None           # Optional emoji or icon name


class PickerItem(BaseModel):
    """A single item in a picker activity."""
    id: str
    label: str
    meta: str | None = None           # Sub-label, e.g. "Completed · 12 May 2026"
    extra: dict[str, Any] | None = None  # Extra data carried back on selection
    children: list['PickerItem'] | None = None # Nested children for accordion/groups


class Activity(BaseModel):
    """The single output type. One per UI primitive emitted by the bot per turn."""

    type: Literal[
        "text",
        "markdown",
        "quick_replies",
        "picker",
        "nested_picker",
        "input",
        "action_button",
        "typing",
        "end",
        "trace",
    ]

    # Text / markdown content
    content: str | None = None

    # quick_replies
    choices: list[QuickReply] | None = None

    # picker
    picker_id: str | None = None
    placeholder: str | None = None
    items: list[PickerItem] | None = None
    other_option: QuickReply | None = None
    search_enabled: bool = True
    total_items: int | None = None

    # action_button
    label: str | None = None           # Button label text, e.g. "Click here to open the course"
    url: str | None = None             # Destination URL the frontend should open

    # input
    input_id: str | None = None
    input_placeholder: str | None = None
    validate_regex: str | None = None

    # end
    outcome: str | None = None        # "self_served" | "ticket_raised" | "ended"

    # trace (admin-only render)
    trace_lines: list[str] | None = None

    # Common UX flag
    disable_input: bool = False

    model_config = {"extra": "forbid"}

    # ---- convenience constructors ----
    @classmethod
    def text(cls, content: str) -> Self:
        return cls(type="text", content=content)

    @classmethod
    def markdown(cls, content: str) -> Self:
        return cls(type="markdown", content=content)

    @classmethod
    def quick_replies(cls, choices: list[QuickReply], disable_input: bool = True) -> Self:
        return cls(type="quick_replies", choices=choices, disable_input=disable_input)

    @classmethod
    def picker(
        cls,
        picker_id: str,
        items: list[PickerItem],
        placeholder: str | None = None,
        other_option: QuickReply | None = None,
    ) -> Self:
        is_nested = any(bool(item.children) for item in items)
        total = (
            sum(len(item.children) for item in items if item.children)
            if is_nested else len(items)
        )
        return cls(
            type="nested_picker" if is_nested else "picker",
            picker_id=picker_id,
            items=items,
            placeholder=placeholder,
            other_option=other_option,
            disable_input=True,
            total_items=total,
        )

    @classmethod
    def action_button(cls, label: str, url: str, content: str | None = None) -> Self:
        """Structured tappable link — frontend opens `url` in WebView / browser.

        Args:
            label:   Button text the user sees, e.g. "Click here to open the course".
            url:     Full destination URL.
            content: Optional introductory text shown above the button.
        """
        return cls(type="action_button", label=label, url=url, content=content)

    @classmethod
    def input(cls, input_id: str, placeholder: str = "") -> Self:
        return cls(type="input", input_id=input_id, input_placeholder=placeholder)

    @classmethod
    def typing(cls) -> Self:
        return cls(type="typing")

    @classmethod
    def end(cls, outcome: str = "ended", content: str | None = None) -> Self:
        return cls(type="end", outcome=outcome, content=content)

    @classmethod
    def trace(cls, lines: list[str]) -> Self:
        return cls(type="trace", trace_lines=lines)


class UserAction(BaseModel):
    """Inbound from the channel — what the user did this turn."""

    action: Literal["start", "send_message", "select_choice", "pick_item", "request_other"]
    session_id: str | None = None

    # send_message
    text: str | None = None

    # select_choice
    choice_id: str | None = None
    user_says: str | None = None      # Display label of choice (for transcript)

    # pick_item
    picker_id: str | None = None
    item_id: str | None = None
    item_label: str | None = None

    # request_other (picker "mine isn't in the list")
    other_query: str | None = None

    model_config = {"extra": "forbid"}
