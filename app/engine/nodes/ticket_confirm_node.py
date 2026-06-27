"""`ticket_confirm` node — show a support-ticket summary and ask the user to confirm.

This is a reusable confirmation step that replaces the ad-hoc ``type: message``
nodes that each flow used to declare individually.  It always emits two standard
quick replies so the UX is consistent across every use-case:

    ✅ Confirm  → routes to ``on_confirm``
    ❌ Cancel   → routes to ``on_cancel``  (default: ``satisfied``)

The second-button label can be overridden via ``cancel_label`` when the flow
needs a friendlier label such as ``✏️ Edit Details``.

YAML shape:

    - id: confirm_ticket_uc1
      type: ticket_confirm
      prompt:
        text: |
          **Issue Identified:** Event Video Missing — Content Configuration Issue

          **Summary of Request:**
          You reported that the video for the event is missing.

          • **Event:** {{ ctx.collected.event_name or 'Selected event' }}
      on_confirm: auto_ticket_uc1
      on_cancel: satisfied          # optional — defaults to "satisfied"
      cancel_label: "✏️ Edit Details"  # optional — defaults to "❌ Cancel"
"""

from __future__ import annotations

from typing import Any, Callable

from langchain_core.messages import AIMessage

from app.engine.activity import Activity, QuickReply
from app.engine.nodes.base import NodeHandler
from app.engine.state import ConversationState, FlowStatus
from app.engine.template import render

_DEFAULT_CANCEL_LABEL = "❌ Cancel"


class TicketConfirmNode(NodeHandler):
    """Reusable ticket-confirmation node with standardised quick replies."""

    node_type = "ticket_confirm"

    def _validate(self, cfg: dict[str, Any]) -> None:
        super()._validate(cfg)
        if "prompt" not in cfg or "text" not in cfg.get("prompt", {}):
            raise ValueError(f"ticket_confirm node {cfg['id']!r} requires 'prompt.text'")
        if "on_confirm" not in cfg:
            raise ValueError(f"ticket_confirm node {cfg['id']!r} requires 'on_confirm'")

    def build(self, cfg: dict[str, Any]) -> Callable[[ConversationState], dict]:
        self._validate(cfg)
        prompt_tmpl: str = cfg["prompt"]["text"]
        cancel_label: str = cfg.get("cancel_label", _DEFAULT_CANCEL_LABEL)
        choices = [
            QuickReply(id="confirm", label="✅ Confirm"),
            QuickReply(id="cancel", label=cancel_label),
        ]

        def run(state: ConversationState) -> dict:
            ctx = {
                "collected": state.collected,
                "counters": state.counters,
                "user_id_hash": state.user_id_hash,
                "channel": state.channel,
            }
            text = render(prompt_tmpl, ctx)

            activities: list[dict] = []
            if text:
                activities.append(Activity.markdown(text).model_dump(exclude_none=True))

            activities.append(
                Activity.quick_replies(choices, disable_input=True).model_dump(
                    exclude_none=True
                )
            )

            return {
                "pending_activities": state.pending_activities + activities,
                "current_node": cfg["id"],
                "status": FlowStatus.AWAITING_USER,
                "messages": [AIMessage(content=text)] if text else [],
            }

        return run

    def next_node(self, cfg: dict[str, Any]) -> str | None:
        return None  # routing is conditional via register_conditional_edges

    def register_conditional_edges(self, graph: Any, cfg: dict[str, Any]) -> bool:
        on_confirm = cfg["on_confirm"]
        on_cancel = cfg.get("on_cancel", "satisfied")
        node_id = cfg["id"]

        def route(state: ConversationState) -> str:
            choice = (state.collected or {}).get("_last_choice_id")
            if choice == "confirm":
                return on_confirm
            return on_cancel

        targets = {on_confirm, on_cancel}
        graph.add_conditional_edges(node_id, route, {t: t for t in targets})
        return True
