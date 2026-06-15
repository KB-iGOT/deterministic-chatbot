"""`open_llm_subgraph` node (Mode D) — agentic course recommendation via LLM.

The user's learning query is passed to the LLM which returns a ranked list of
course recommendations. Results are stored in `collected[store_as]` and the flow
continues to a deterministic presentation node. On any failure the flow routes
to the `on_error` path (deterministic fallback — never blocks the user).

YAML shape:
    - id: invoke_recommender
      type: open_llm_subgraph
      subgraph: course_recommender          # logical name (used for logging/metrics)
      inputs:
        query:        "{{ ctx.collected.user_query }}"
        user_id_hash: "{{ ctx.user_id_hash }}"
        language:     "{{ ctx.language | default('en') }}"
        max_results:  5
      timeout_seconds: 30
      max_llm_calls: 5
      store_as: collected.recommendations   # where to write the result list
      on_success: present_recommendations
      on_error:
        timeout: fallback_template_search
        any:     fallback_template_search
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from app.config import settings
from app.engine.nodes.base import NodeHandler
from app.engine.state import ConversationState
from app.engine.template import render

log = logging.getLogger(__name__)

_SUBGRAPH_RESULT_KEY = "_llm_subgraph_result"  # "success" | "error"


class OpenLLMSubgraphNode(NodeHandler):
    node_type = "open_llm_subgraph"

    def _validate(self, cfg: dict[str, Any]) -> None:
        super()._validate(cfg)
        if "store_as" not in cfg:
            raise ValueError(f"open_llm_subgraph node {cfg['id']!r} requires 'store_as'")
        if "on_success" not in cfg:
            raise ValueError(f"open_llm_subgraph node {cfg['id']!r} requires 'on_success'")
        if "on_error" not in cfg:
            raise ValueError(f"open_llm_subgraph node {cfg['id']!r} requires 'on_error'")

    def build(self, cfg: dict[str, Any]) -> Callable[[ConversationState], dict]:
        self._validate(cfg)

        inputs_cfg: dict[str, Any] = cfg.get("inputs", {})
        store_as: str = cfg["store_as"]
        on_success: str = cfg["on_success"]
        on_error_any: str = (cfg.get("on_error") or {}).get("any", on_success)
        max_results: int = int(inputs_cfg.get("max_results", 5))
        subgraph_name: str = cfg.get("subgraph", "course_recommender")

        # Derive the collected field name from store_as ("collected.recommendations" → "recommendations")
        store_field = store_as.removeprefix("collected.")

        async def run(state: ConversationState) -> dict:
            llm_adapter = self.services.get("llm")
            collected = dict(state.collected or {})

            def _error(reason: str) -> dict:
                log.warning("open_llm_subgraph[%s/%s]: %s → on_error", cfg["id"], subgraph_name, reason)
                collected[_SUBGRAPH_RESULT_KEY] = "error"
                return {"collected": collected, "current_node": cfg["id"]}

            # Kill-switch / LLM unavailable
            if settings.llm_kill_switch or llm_adapter is None:
                return _error("LLM disabled or unavailable")

            # Per-session call cap
            cap = settings.llm_max_calls_per_session
            if state.llm_calls_this_session >= cap:
                return _error(f"session LLM cap reached ({cap})")

            # Render the query template
            ctx_data = {
                "collected": collected,
                "user_id_hash": state.user_id_hash,
                "channel": str(state.channel),
                "language": state.language,
                "session_id": str(state.session_id),
            }

            query_template = inputs_cfg.get("query", "")
            query = render(str(query_template), ctx_data).strip()

            if not query:
                return _error("empty query after rendering")

            log.info(
                "open_llm_subgraph[%s/%s]: query=%r max_results=%d",
                cfg["id"], subgraph_name, query[:120], max_results,
            )

            # Optionally supply user's enrolments as negative context
            # (avoids re-recommending courses the user is already on)
            context_courses: list[dict] | None = None
            karmayogi = self.services.get("karmayogi")
            if karmayogi is not None:
                try:
                    user_id = render("{{ ctx.user_id_hash }}", ctx_data)
                    resp = await karmayogi.execute_request(
                        method="POST",
                        url=f"/api/course/private/v4/user/enrollment/list/{user_id}",
                        body={"request": {"filters": {}}},
                    )
                    context_courses = (resp or {}).get("courses") or []
                    log.debug(
                        "open_llm_subgraph: fetched %d existing enrolments for context",
                        len(context_courses),
                    )
                except Exception:  # noqa: BLE001
                    log.debug("open_llm_subgraph: could not fetch enrolments (non-fatal)")

            # Call LLM
            try:
                recommendations = await llm_adapter.generate_recommendations(
                    query=query,
                    context_courses=context_courses,
                    max_results=max_results,
                )
            except Exception:  # noqa: BLE001
                log.exception("open_llm_subgraph[%s]: LLM call failed", cfg["id"])
                return _error("LLM call failed")

            if not isinstance(recommendations, list) or not recommendations:
                return _error("LLM returned empty or non-list recommendations")

            log.info(
                "open_llm_subgraph[%s]: got %d recommendations for query %r",
                cfg["id"], len(recommendations), query[:60],
            )

            collected[store_field] = recommendations
            collected[_SUBGRAPH_RESULT_KEY] = "success"

            return {
                "collected": collected,
                "current_node": cfg["id"],
                "llm_calls_this_session": state.llm_calls_this_session + 1,
            }

        return run

    def next_node(self, cfg: dict[str, Any]) -> str | None:
        # Routing is always conditional (success vs error path).
        return None

    def register_conditional_edges(self, graph: Any, cfg: dict[str, Any]) -> bool:
        """Wire conditional edges: on_success or on_error.any."""
        on_success: str = cfg["on_success"]
        on_error_any: str = (cfg.get("on_error") or {}).get("any", on_success)
        node_id = cfg["id"]

        targets = {on_success, on_error_any}

        def route(state: ConversationState) -> str:
            result = (state.collected or {}).get(_SUBGRAPH_RESULT_KEY, "error")
            if result == "success":
                return on_success
            return on_error_any

        graph.add_conditional_edges(node_id, route, {t: t for t in targets})
        return True
