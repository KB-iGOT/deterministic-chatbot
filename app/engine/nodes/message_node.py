"""`message` node — bot says something + optional quick_replies.

YAML shape:
    - id: greet_user
      type: message
      prompt:
        text: "Hi {{ ctx.collected.user_name | default('there') }} 👋"
        voice: "Hi there, welcome."  # Phase 4 optional
      quick_replies:
        - { id: CERT, label: "Certificate help", spoken_label: "certificate", dtmf: "1" }
        - { id: PROF, label: "Profile help",     spoken_label: "profile",     dtmf: "2" }
      disable_input: true
      on_reply:
        save_to: collected.issue_category
        next: branch_on_category
      # OR for messages without quick_replies, just `next: <node_id>`

    # Optional — emit a tappable course/URL button after the message text:
      action_button:
        label: "Click here to open the course"
        url: "{{ env.KARMAYOGI_PORTAL_BASE_URL }}/app/toc/{{ ctx.collected.selected_course_id }}/overview"
"""

from __future__ import annotations

from typing import Any, Callable

from langchain_core.messages import AIMessage

from app.engine.activity import Activity, QuickReply
from app.engine.nodes.base import NodeHandler
from app.engine.state import ConversationState, FlowStatus
from app.engine.template import render


class MessageNode(NodeHandler):
    node_type = "message"

    def _validate(self, cfg: dict[str, Any]) -> None:
        super()._validate(cfg)
        if "prompt" not in cfg:
            raise ValueError(f"message node {cfg['id']!r} missing 'prompt'")
        if "quick_replies" in cfg and "on_reply" not in cfg:
            raise ValueError(
                f"message node {cfg['id']!r} has quick_replies but no on_reply"
            )

    def build(self, cfg: dict[str, Any]) -> Callable[[ConversationState], dict]:
        self._validate(cfg)

        prompt_text_tmpl: str = cfg["prompt"].get("text", "")
        prompt_voice_tmpl: str | None = cfg["prompt"].get("voice")
        quick_replies_raw: list[dict] | None = cfg.get("quick_replies")
        action_button_raw: dict | None = cfg.get("action_button")
        disable_input: bool = cfg.get("disable_input", bool(quick_replies_raw))

        def run(state: ConversationState) -> dict:
            ctx = {
                "collected": state.collected,
                "counters": state.counters,
                "user_id_hash": state.user_id_hash,
                "channel": state.channel,
            }
            text = render(prompt_text_tmpl, ctx) if prompt_text_tmpl else ""

            activities: list[dict] = []
            if text:
                activities.append(
                    Activity.markdown(text).model_dump(exclude_none=True)
                )

            if quick_replies_raw:
                choices = [
                    QuickReply(
                        id=qr["id"],
                        label=qr["label"],
                        spoken_label=qr.get("spoken_label"),
                        dtmf=qr.get("dtmf"),
                        icon=qr.get("icon"),
                    )
                    for qr in quick_replies_raw
                ]
                activities.append(
                    Activity.quick_replies(choices, disable_input=disable_input).model_dump(
                        exclude_none=True
                    )
                )

            if action_button_raw:
                btn_label = render(action_button_raw.get("label", ""), ctx)
                btn_url   = render(action_button_raw.get("url", ""), ctx)
                if btn_label and btn_url:
                    activities.append(
                        Activity.action_button(label=btn_label, url=btn_url).model_dump(
                            exclude_none=True
                        )
                    )

            # Append bot message to conversation history for LLM transcript
            bot_messages = [AIMessage(content=text)] if text else []

            return {
                "pending_activities": state.pending_activities + activities,
                "current_node": cfg["id"],
                "status": FlowStatus.AWAITING_USER if quick_replies_raw else FlowStatus.ACTIVE,
                "messages": bot_messages,
            }

        return run

    def next_node(self, cfg: dict[str, Any]) -> str | None:
        # If quick_replies, next is determined by on_reply at runtime — caller registers
        # conditional edges. Otherwise simple next-edge.
        if cfg.get("quick_replies"):
            return None
        return cfg.get("next")
