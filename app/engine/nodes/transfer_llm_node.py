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
            import asyncio as _asyncio
            llm_adapter = self.services.get("llm")
            presidio = self.services.get("presidio")
            from app.config import settings

            # Always build the template draft first — instant, always populated
            draft = _build_template_draft(state, cfg, llm_directives)
            llm_used = False

            # Try LLM to enhance the draft (only if not already capped / kill-switched)
            if (
                state.llm_calls_this_session < 1
                and not settings.llm_kill_switch
                and llm_adapter is not None
            ):
                llm_input = _build_llm_input(state, llm_context_cfg, llm_directives, presidio)
                try:
                    draft_dict = await _asyncio.wait_for(
                        llm_adapter.generate_ticket_summary(
                            transcript=llm_input["transcript"],
                            collected=llm_input["collected"],
                            flow_meta=llm_input["flow_meta"],
                            directives=llm_directives,
                        ),
                        timeout=float(settings.llm_timeout_seconds),
                    )
                    candidate = TicketDraft(**draft_dict)
                    if candidate.subject and candidate.description:
                        # Carry over the template-built conversation trail — LLM doesn't return it
                        draft = candidate.model_copy(update={
                            "conversation_trail": draft.conversation_trail,
                        })
                        llm_used = True
                except Exception:  # noqa: BLE001
                    pass  # keep template draft

            if auto_raise:
                # Mode B auto-raise: show brief acknowledgement, proceed to on_complete
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
                    "llm_calls_this_session": state.llm_calls_this_session + (1 if llm_used else 0),
                }


            # Standard mode: show draft + ask for confirmation
            summary_msg = (
                f"📋 **Issue Identified:** {draft.subject}\n\n"
                f"{draft.description[:500]}\n\n"
                f"Please confirm if the above details are correct."
            )

            activities = [
                Activity.markdown(summary_msg).model_dump(exclude_none=True),
                Activity.quick_replies(
                    choices=[
                        {"id": "confirm", "label": "✅ Confirm"},
                        {"id": "restart", "label": "🔄 Discard & Restart"},
                    ]
                ).model_dump(exclude_none=True),
            ]

            return {
                "ticket_draft": draft.model_dump(),
                "pending_activities": state.pending_activities + activities,
                "current_node": cfg["id"],
                "status": FlowStatus.ESCALATING,
                "llm_calls_this_session": state.llm_calls_this_session + (1 if llm_used else 0),
            }

        return run

    def next_node(self, cfg: dict[str, Any]) -> str | None:
        # Standard mode uses conditional edges (register_conditional_edges returns True)
        if not cfg.get("auto_raise", False):
            return None
        return cfg.get("on_complete")

    def register_conditional_edges(self, graph: Any, cfg: dict[str, Any]) -> bool:
        if cfg.get("auto_raise", False):
            return False

        from langgraph.graph import END

        on_complete = cfg["on_complete"]
        on_restart = cfg.get("on_restart")

        def _route(state: ConversationState) -> str:
            choice = (state.collected or {}).get("_last_choice_id", "")
            if choice == "restart" and on_restart:
                return on_restart
            return on_complete

        targets = {on_complete: on_complete}
        if on_restart:
            targets[on_restart] = on_restart
        else:
            targets[END] = END

        graph.add_conditional_edges(cfg["id"], _route, targets)
        return True


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


_INTERNAL_KEY_PREFIXES = (
    "_", "sub_scenario", "report_error", "leaderboard_issue",
    "bulk_issue", "bulk_error", "user_role", "otp_channel",
    "user_found_count", "fetched_user_id", "root_org_id", "org_channel",
    "profile_status", "org_search_count",
)

_READABLE_KEYS = {
    # Common
    "user_id":              "User ID",
    "email":                "Email",
    "mobile":               "Phone",
    "first_name":           "First Name",
    "last_name":            "Last Name",
    "user_email":           "Email",
    "user_primary_email":   "Primary Email",
    "org_name":             "Organisation",
    "transfer_dept_name":   "Target Organisation",
    "identifier":           "Identifier",
    "update_type":          "Update Type",
    # Course / content
    "course_name":          "Course",
    "course_id":            "Course ID",
    "resource_name":        "Resource",
    "resource_type":        "Resource Type",
    # Device
    "device_name":          "Device Name",
    "device_model":         "Device Model",
    "android_version":      "Android Version",
    "ios_version":          "iOS Version",
    # Certificate flow (C1/C3)
    "c1_course_name":       "Course",
    "c1_course_id":         "Course ID",
    "c1_enrollment_status": "Enrollment Status",
    "c1_completion_pct":    "Completion %",
    "c1_completed_on_iso":  "Completed On",
    "c3_first_name":        "Name (First)",
    "c3_last_name":         "Name (Last)",
    # CAP / APAR
    "cap_name":             "CAP Name",
    "ec1_course_name":      "Course (APAR)",
    # Weekly clap
    "total_claps":          "Total Claps",
    "w1_label":             "Week 1",
    "w2_label":             "Week 2",
    "w3_label":             "Week 3",
    "w4_label":             "Week 4",
}


def _build_template_draft(
    state: ConversationState,
    cfg: dict[str, Any],
    directives: dict[str, Any],
) -> TicketDraft:
    """Build a clean, readable ticket draft from collected fields — no LLM involved.

    Used as the baseline for the confirmation preview and as fallback for Zoho
    when the LLM is unavailable or returns empty fields.
    """
    collected = state.collected or {}
    priority = directives.get("priority_override", "P3")

    # Fields to exclude from the summary body — either in the header table or internal routing
    _HEADER_FIELDS = {"email", "mobile", "first_name", "last_name", "user_id"}
    _SKIP_FIELDS   = {"category", "device_type", "youtube_restricted"}

    field_items: list[tuple[str, Any]] = []
    for k, v in collected.items():
        if any(k.startswith(p) for p in _INTERNAL_KEY_PREFIXES):
            continue
        if v is None or v == "" or v == [] or v == {}:
            continue
        if k in _HEADER_FIELDS or k in _SKIP_FIELDS:
            continue
        label = _READABLE_KEYS.get(k, k.replace("_", " ").title())
        field_items.append((label, v))

    flow_label = (state.flow_id or "").replace("_", " ").title()
    subject = directives.get("subject_hint") or _derive_subject(flow_label, collected)

    # Plain text — no markdown symbols. Renders in chat and embeds cleanly in Zoho HTML.
    if field_items:
        description = "Details collected:\n\n" + "\n".join(
            f"{label}: {v}" for label, v in field_items
        )
    else:
        description = "No additional details collected."

    return TicketDraft(
        subject=subject,
        description=description,
        category=collected.get("category", "General"),
        sub_category=collected.get("sub_scenario"),
        priority=priority,
        severity="Sev 3",
        conversation_trail=_build_conversation_trail(state),
    )


def _derive_subject(flow_label: str, collected: dict[str, Any]) -> str:
    """Build a specific ticket subject from collected context fields."""
    course = (
        collected.get("c1_course_name")
        or collected.get("ec1_course_name")
        or collected.get("course_name")
    )
    resource = collected.get("resource_name")
    cap = collected.get("cap_name")

    if resource and course:
        return f"{flow_label} — {course} / {resource}"
    if course:
        return f"{flow_label} — {course}"
    if cap:
        return f"{flow_label} — {cap}"
    return f"{flow_label} — Support Request"


def _build_conversation_trail(state: ConversationState) -> str:
    """Build an HTML <ol> of key-value pairs from the conversation steps.

    Steps are recorded in collected['_user_steps'] by routes.py as
    'Question Label: User Answer'. Keys are rendered bold in Zoho HTML.
    """
    steps = (state.collected or {}).get("_user_steps") or []
    if not steps or not isinstance(steps, list):
        return ""
    items = []
    for step in steps:
        if not isinstance(step, str) or not step.strip():
            continue
        safe = step.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if ": " in safe:
            key, _, val = safe.partition(": ")
            items.append(f"<li><b>{key}:</b> {val}</li>")
        else:
            items.append(f"<li>{safe}</li>")
    if not items:
        return ""
    return "<ol>" + "".join(items) + "</ol>"


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
            f"📋 **Issue Identified:** {draft.subject}\n\n"
            f"**Summary of Request:**\n{draft.description[:400]}\n\n"
            f"**Category:** {draft.category}\n"
            f"**Priority:** {draft.priority}\n\n"
            f"Please review the details above and confirm whether they are correct."
        ).model_dump(exclude_none=True),
        Activity.quick_replies(
            choices=[
                {"id": "confirm", "label": "✅ Confirm"},
                {"id": "restart", "label": "🔄 Discard & Restart"},
            ]
        ).model_dump(exclude_none=True),
    ]

    return {
        "ticket_draft": draft.model_dump(),
        "pending_activities": state.pending_activities + activities,
        "current_node": cfg["id"],
        "status": FlowStatus.ESCALATING,
    }
