"""`transfer_llm` node — Mode B handover. LLM paraphrases conversation → ticket summary.

THIS IS THE ONLY NODE TYPE IN PHASE 1 THAT CALLS THE LLM.
CI rule: deterministic node types must not import the LLM adapter.

YAML shape (standard — asks user to confirm before raising):

    - id: transfer_to_llm
      type: transfer_llm
      llm_context:
        include_messages: true
        include_collected: true
        include_flow_meta: true
      llm_directives:
        objective: |
          The user has been through 2+ deterministic attempts and remains unsatisfied.
          Do NOT try to re-resolve. Your job is to:
            1. Acknowledge briefly.
            2. Confirm core issue.
            3. Draft Zoho ticket.
            4. Confirm with user → create ticket.
        priority_override: P3
        ticket_tags: [fallback_from_deterministic, user_escalation]
      on_complete: end

YAML shape (auto_raise — generates ticket summary and proceeds immediately, no user confirmation):

    - id: auto_ticket_summary
      type: transfer_llm
      auto_raise: true          # skip user confirmation; proceed to on_complete immediately
      llm_context:
        include_messages: true
        include_collected: true
      llm_directives:
        objective: |
          The user could not resolve their issue through self-service steps.
          Generate a concise Zoho support ticket.
          Subject: short description of the issue.
          Description: include all key data from the conversation (course, issue type, dates, etc.).
        priority_override: P4
      on_complete: confirm_ticket   # flows to _zoho_ticket fragment node
"""

from __future__ import annotations

from typing import Any, Callable

from app.engine.activity import Activity
from app.engine.nodes.base import NodeHandler
from app.engine.state import ConversationState, FlowStatus, TicketDraft
from app.engine.template import render


class TransferLLMNode(NodeHandler):
    node_type = "transfer_llm"

    def _validate(self, cfg: dict[str, Any]) -> None:
        super()._validate(cfg)
        if "llm_directives" not in cfg:
            raise ValueError(
                f"transfer_llm node {cfg['id']!r} requires 'llm_directives'"
            )
        if "on_complete" not in cfg:
            raise ValueError(
                f"transfer_llm node {cfg['id']!r} requires 'on_complete'"
            )

    def build(self, cfg: dict[str, Any]) -> Callable[[ConversationState], dict]:
        self._validate(cfg)

        auto_raise: bool = bool(cfg.get("auto_raise", False))
        llm_context_cfg = cfg.get("llm_context", {})
        llm_directives = cfg["llm_directives"]

        async def run(state: ConversationState) -> dict:
            llm_adapter = self.services.get("llm")
            presidio = self.services.get("presidio")

            # Per-session LLM call cap (cost control)
            if state.llm_calls_this_session >= 1:
                return _fallback_to_template(state, cfg, reason="session_cap", auto_raise=auto_raise)

            # Kill-switch
            from app.config import settings

            if settings.llm_kill_switch or llm_adapter is None:
                return _fallback_to_template(state, cfg, reason="llm_disabled_or_unavailable", auto_raise=auto_raise)

            # Build LLM input from llm_context config
            llm_input = _build_llm_input(state, llm_context_cfg, llm_directives, presidio)

            try:
                draft_dict = await llm_adapter.generate_ticket_summary(
                    transcript=llm_input["transcript"],
                    collected=llm_input["collected"],
                    flow_meta=llm_input["flow_meta"],
                    directives=llm_directives,
                )
                draft = TicketDraft(**draft_dict)
            except Exception:  # noqa: BLE001
                # Graceful fallback to template summary
                return _fallback_to_template(state, cfg, reason="llm_call_failed", auto_raise=auto_raise)

            if auto_raise:
                # Mode B auto-raise: generate draft, show brief acknowledgement,
                # then proceed directly to on_complete (confirm_ticket) without
                # asking the user to confirm.
                activities = [
                    Activity.markdown(
                        "📋 I wasn't able to resolve your issue through the self-service steps.\n\n"
                        "Creating a support ticket for the L2 team now — "
                        "they will reach out within **2 business days**."
                    ).model_dump(exclude_none=True),
                ]
                return {
                    "ticket_draft": draft.model_dump(),
                    "pending_activities": state.pending_activities + activities,
                    "current_node": cfg["on_complete"],
                    "status": FlowStatus.ACTIVE,
                    "llm_calls_this_session": state.llm_calls_this_session + 1,
                }

            # Standard mode: show draft + ask for confirmation
            summary_msg = render(
                "📋 Here's what I'll raise as a support ticket:\n\n"
                "**Subject:** {{ subject }}\n"
                "**Category:** {{ category }} → {{ sub_category | default('-') }}\n"
                "**Priority:** {{ priority }}\n\n"
                "{{ description }}\n\n"
                "Confirm to raise.",
                {
                    "subject": draft.subject,
                    "category": draft.category,
                    "sub_category": draft.sub_category,
                    "priority": draft.priority,
                    "description": draft.description[:300],
                },
            )

            activities = [
                Activity.markdown(summary_msg).model_dump(exclude_none=True),
                Activity.quick_replies(
                    choices=[
                        {"id": "confirm", "label": "✅ Yes — raise ticket"},
                        {"id": "edit", "label": "✏️ Let me correct something"},
                    ]
                ).model_dump(exclude_none=True),
            ]

            return {
                "ticket_draft": draft.model_dump(),
                "pending_activities": state.pending_activities + activities,
                "current_node": cfg["id"],
                "status": FlowStatus.ESCALATING,
                "llm_calls_this_session": state.llm_calls_this_session + 1,
            }

        return run

    def next_node(self, cfg: dict[str, Any]) -> str | None:
        return cfg.get("on_complete")


def _build_llm_input(
    state: ConversationState,
    ctx_cfg: dict[str, Any],
    directives: dict[str, Any],
    presidio: Any,
) -> dict[str, Any]:
    """Assemble LLM input, applying Presidio PII redaction."""
    transcript_lines = []
    if ctx_cfg.get("include_messages", True):
        for m in state.messages or []:
            role = "User" if getattr(m, "type", "") == "human" else "Bot"
            content = getattr(m, "content", "")
            transcript_lines.append(f"{role}: {content}")

    transcript = "\n".join(transcript_lines)

    # PII redaction
    if presidio is not None:
        transcript = presidio.redact(transcript)

    # Strip internal routing keys before sending to LLM.
    # These are flow-engine bookkeeping fields — not meaningful to the ticket summary
    # and can cause the LLM to leak internal IDs (e.g. "sub_scenario L1") into descriptions.
    _INTERNAL_KEY_PREFIXES = ("_", "sub_scenario", "report_error", "leaderboard_issue",
                               "bulk_issue", "bulk_error", "user_role", "otp_channel")
    raw_collected = state.collected if ctx_cfg.get("include_collected", True) else {}
    collected_for_llm = {
        k: v for k, v in (raw_collected or {}).items()
        if not any(k.startswith(p) for p in _INTERNAL_KEY_PREFIXES)
    }
    flow_meta = {
        "flow_id": state.flow_id,
        "channel": str(state.channel),
        "directives": directives,
    } if ctx_cfg.get("include_flow_meta", True) else {}

    return {
        "transcript": transcript,
        "collected": collected_for_llm,
        "flow_meta": flow_meta,
    }


def _fallback_to_template(
    state: ConversationState,
    cfg: dict[str, Any],
    reason: str,
    auto_raise: bool = False,
) -> dict[str, Any]:
    """Build a deterministic template-based ticket summary instead of calling LLM.

    This is the safety net: escalation never blocks on LLM availability.
    When auto_raise=True, skips user confirmation and proceeds directly to on_complete.
    """
    last_user_turns = [
        getattr(m, "content", "")
        for m in (state.messages or [])
        if getattr(m, "type", "") == "human"
    ][-3:]

    description_parts = [
        f"Flow: {state.flow_id}",
        f"Last user turns: {' | '.join(last_user_turns) or '(none)'}",
        "",
        "Collected data:",
    ]
    for k, v in (state.collected or {}).items():
        description_parts.append(f"  - {k}: {v}")
    description_parts.append("")
    description_parts.append(f"[Auto-summary; reason: {reason}]")

    draft = TicketDraft(
        subject=f"{state.flow_id} — escalation",
        description="\n".join(description_parts),
        category=state.collected.get("category", "GENERAL_INQUIRY") if state.collected else "GENERAL_INQUIRY",
        sub_category=state.collected.get("sub_scenario"),
        priority="P3",
        severity="Sev 3",
    )

    if auto_raise:
        # No user confirmation — just inform and proceed
        activities = [
            Activity.markdown(
                "📋 I wasn't able to resolve your issue through the self-service steps.\n\n"
                "Creating a support ticket now — the L2 team will reach out within **2 business days**."
            ).model_dump(exclude_none=True),
        ]
        return {
            "ticket_draft": draft.model_dump(),
            "pending_activities": state.pending_activities + activities,
            "current_node": cfg["on_complete"],
            "status": FlowStatus.ACTIVE,
        }

    # Standard mode: show summary + ask for confirmation
    activities = [
        Activity.markdown(
            f"📋 I'll raise a support ticket with the conversation details:\n\n"
            f"**Subject:** {draft.subject}\n\n"
            f"Confirm to raise."
        ).model_dump(exclude_none=True),
        Activity.quick_replies(
            choices=[
                {"id": "confirm", "label": "✅ Yes — raise ticket"},
                {"id": "edit", "label": "✏️ Let me correct something"},
            ]
        ).model_dump(exclude_none=True),
    ]

    return {
        "ticket_draft": draft.model_dump(),
        "pending_activities": state.pending_activities + activities,
        "current_node": cfg["id"],
        "status": FlowStatus.ESCALATING,
    }
