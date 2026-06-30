"""`resolution` node — show numbered steps + satisfaction follow-up.

YAML shape:
    - id: resolution_cert_pending
      type: resolution
      prompt:
        text: "Your certificate is being generated. This can take up to 24 hours."
      steps:
        - "Check back in 24 hours."
        - "Ensure all course modules are marked complete."
        - "If 24 hours have passed, raise a ticket and we'll investigate."
      follow_up:
        text: "How long has it been since you completed the course?"
        quick_replies:
          - { id: lt_24h, label: "Less than 24 hours", dtmf: "1" }
          - { id: gt_24h, label: "More than 24 hours", dtmf: "2" }
      on_reply:
        lt_24h: end_wait_24h
        gt_24h: collect_ticket_details
"""

from __future__ import annotations

from typing import Any, Callable

from langchain_core.messages import AIMessage

from app.engine.activity import Activity, QuickReply
from app.engine.nodes.base import NodeHandler
from app.engine.state import ConversationState, FlowStatus
from app.engine.template import render


class ResolutionNode(NodeHandler):
    node_type = "resolution"

    def _validate(self, cfg: dict[str, Any]) -> None:
        super()._validate(cfg)
        if "steps" not in cfg or not isinstance(cfg["steps"], list):
            raise ValueError(f"resolution node {cfg['id']!r} requires 'steps' list")

    def build(self, cfg: dict[str, Any]) -> Callable[[ConversationState], dict]:
        self._validate(cfg)

        prompt = cfg.get("prompt", {})
        steps = cfg["steps"]
        follow_up = cfg.get("follow_up")
        action_button_raw: dict | None = cfg.get("action_button")

        def run(state: ConversationState) -> dict:
            ctx = {
                "collected": state.collected,
                "counters": state.counters,
                "user_id_hash": state.user_id_hash,
                "channel": state.channel,
            }

            activities: list[dict] = []
            fu_text: str = ""  # populated below if follow_up is present
            intro = render(prompt.get("text", ""), ctx) if prompt else ""

            body_lines: list[str] = []
            if intro:
                body_lines.append(intro)
            body_lines.append("")
            for i, step in enumerate(steps, start=1):
                body_lines.append(f"{i}. {render(step, ctx)}")

            activities.append(Activity.markdown("\n".join(body_lines)).model_dump(exclude_none=True))

            if action_button_raw:
                btn_label = render(action_button_raw.get("label", ""), ctx)
                btn_url   = render(action_button_raw.get("url", ""), ctx)
                if btn_label and btn_url:
                    activities.append(
                        Activity.action_button(label=btn_label, url=btn_url).model_dump(
                            exclude_none=True
                        )
                    )

            if follow_up:
                fu_text = render(follow_up.get("text", ""), ctx)
                if fu_text:
                    activities.append(Activity.markdown(fu_text).model_dump(exclude_none=True))
                fu_qr = follow_up.get("quick_replies")
                if fu_qr:
                    choices = [
                        QuickReply(
                            id=qr["id"],
                            label=qr["label"],
                            spoken_label=qr.get("spoken_label"),
                            dtmf=qr.get("dtmf"),
                            icon=qr.get("icon"),
                        )
                        for qr in fu_qr
                    ]
                    activities.append(
                        Activity.quick_replies(choices).model_dump(exclude_none=True)
                    )

            # Compose full bot text for the conversation history transcript.
            all_bot_text = "\n".join(body_lines)
            if fu_text:
                all_bot_text = f"{all_bot_text}\n{fu_text}"

            return {
                "pending_activities": state.pending_activities + activities,
                "current_node": cfg["id"],
                "status": FlowStatus.AWAITING_USER if follow_up else FlowStatus.ACTIVE,
                "messages": [AIMessage(content=all_bot_text)] if all_bot_text.strip() else [],
            }

        return run

    def next_node(self, cfg: dict[str, Any]) -> str | None:
        # If on_reply, conditional edges. Else simple next.
        if cfg.get("on_reply"):
            return None
        return cfg.get("next")
