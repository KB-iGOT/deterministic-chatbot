"""`engineering_ticket` node — Insert a ticket directly to PostgreSQL.

This node replaces the legacy Excel sheet approach with a robust PostgreSQL 
database table. It is specifically used for logging support tickets related 
to Course Progress issues and CAP (Capacity Building) flows.

YAML shape:

    - id: technical_issue_auto_ticket
      type: engineering_ticket
      user_id: "{{ ctx.user_id_hash }}"
      course_do_id: "{{ ctx.collected.course_id }}"
      content_do_id: "{{ ctx.collected.content_do_id }}"
      on_success: technical_issue_ticket_confirm
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from app.engine.nodes.base import NodeHandler
from app.engine.state import ConversationState
from app.engine.template import render

log = logging.getLogger(__name__)

class EngineeringTicketNode(NodeHandler):
    node_type = "engineering_ticket"

    def _validate(self, cfg: dict[str, Any]) -> None:
        super()._validate(cfg)
        for field in ("user_id", "course_do_id", "content_do_id", "on_success"):
            if field not in cfg:
                raise ValueError(
                    f"engineering_ticket node {cfg['id']!r} requires '{field}'"
                )

    def build(self, cfg: dict[str, Any]) -> Callable[[ConversationState], dict]:
        self._validate(cfg)

        user_id_tmpl: str = cfg["user_id"]
        course_do_id_tmpl: str = cfg["course_do_id"]
        content_do_id_tmpl: str = cfg["content_do_id"]

        services = self.services  # captured from constructor

        async def run(state: ConversationState) -> dict:
            node_id = cfg["id"]

            ctx = {
                "collected": state.collected,
                "counters": state.counters,
                "user_id_hash": state.user_id_hash,
                "channel": state.channel,
            }

            try:
                user_id = render(user_id_tmpl, ctx)
                course_do_id = render(course_do_id_tmpl, ctx)
                content_do_id = render(content_do_id_tmpl, ctx)
            except Exception as exc:
                log.warning("[engineering_ticket] node=%s field render failed: %s", node_id, exc)
                return _record_error(state, cfg, f"render_failed:{exc}")

            svc = services.get("engineering_db")
            if svc is None:
                log.error("[engineering_ticket] node=%s service 'engineering_db' not in registry", node_id)
                return _record_error(state, cfg, "service_not_found")

            try:
                await svc.insert_ticket(user_id, course_do_id, content_do_id)
            except Exception as exc:
                log.exception("[engineering_ticket] node=%s insert_ticket() raised: %s", node_id, exc)
                return _record_error(state, cfg, f"insert_error:{exc}")

            new_collected = dict(state.collected)
            # Create a fake ticket_id for now so the UI doesn't break
            new_collected["ticket_id"] = "ENG-TICKET"
            new_collected.pop("_last_api_error", None)

            return {
                "current_node": node_id,
                "collected": new_collected,
            }

        return run

    def next_node(self, cfg: dict[str, Any]) -> str | None:
        return cfg.get("on_success")

    def register_conditional_edges(self, graph: Any, cfg: dict[str, Any]) -> bool:
        on_error = cfg.get("on_error")
        if not isinstance(on_error, dict):
            return False

        on_success = cfg["on_success"]
        node_id = cfg["id"]

        def route(state: ConversationState) -> str:
            err = (state.collected or {}).get("_last_api_error", "")
            if not err:
                return on_success
            for key, target in on_error.items():
                if err.startswith(key + ":") or err == key:
                    return target
            return on_error.get("any", on_success)

        targets = {on_success, *on_error.values()}
        graph.add_conditional_edges(node_id, route, {t: t for t in targets})
        return True


def _record_error(
    state: ConversationState, cfg: dict[str, Any], error: str
) -> dict[str, Any]:
    log.warning(
        "[engineering_ticket] node=%s recording error → %r",
        cfg["id"], error,
    )
    return {
        "current_node": cfg["id"],
        "collected": {**state.collected, "_last_api_error": error},
    }
