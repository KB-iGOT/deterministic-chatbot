"""`llm_choose` node (Mode C) — LLM classifies free text into one of N candidate node IDs.

When the user's free-text description is ambiguous the LLM picks the best matching
sub-intent from a constrained list. Output is always one of the declared candidate IDs
— the LLM cannot invent a new branch. Low-confidence or failed calls route to
`on_low_confidence` instead.

YAML shape:
    - id: classify_course_problem
      type: llm_choose
      input: "{{ ctx.collected.user_description }}"
      candidates:
        - { id: dispatch_progress_issue,   criteria: "User says progress isn't updating" }
        - { id: dispatch_certificate_issue, criteria: "User mentions certificate" }
        - { id: dispatch_content_issue,    criteria: "User can't open / view content" }
        - { id: dispatch_assessment_issue, criteria: "User mentions test, quiz, assessment" }
      confidence_threshold: 0.8        # optional, default 0.7
      on_low_confidence: ask_clarification
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from app.config import settings
from app.engine.nodes.base import NodeHandler
from app.engine.state import ConversationState
from app.engine.template import render

log = logging.getLogger(__name__)


class LLMChooseNode(NodeHandler):
    node_type = "llm_choose"

    def _validate(self, cfg: dict[str, Any]) -> None:
        super()._validate(cfg)
        if "candidates" not in cfg or not cfg["candidates"]:
            raise ValueError(f"llm_choose node {cfg['id']!r} requires non-empty 'candidates'")
        if "input" not in cfg:
            raise ValueError(f"llm_choose node {cfg['id']!r} requires 'input' template")
        if "on_low_confidence" not in cfg:
            raise ValueError(f"llm_choose node {cfg['id']!r} requires 'on_low_confidence'")

    def build(self, cfg: dict[str, Any]) -> Callable[[ConversationState], dict]:
        self._validate(cfg)

        candidates = cfg["candidates"]
        threshold = float(cfg.get("confidence_threshold", 0.7))
        on_low_confidence = cfg["on_low_confidence"]
        input_template = cfg["input"]
        valid_ids = {c["id"] for c in candidates}

        async def run(state: ConversationState) -> dict:
            llm_adapter = self.services.get("llm")
            collected = dict(state.collected or {})

            def _route_low(reason: str) -> dict:
                log.info("llm_choose[%s]: %s → on_low_confidence", cfg["id"], reason)
                collected["_llm_choice"] = on_low_confidence
                collected["_llm_confidence"] = 0.0
                return {"collected": collected, "current_node": cfg["id"]}

            # Kill-switch / LLM unavailable
            if settings.llm_kill_switch or llm_adapter is None:
                return _route_low("LLM disabled or unavailable")

            # Per-session call cap
            cap = settings.llm_max_calls_per_session
            if state.llm_calls_this_session >= cap:
                return _route_low(f"session LLM cap reached ({cap})")

            # Render input template
            ctx_data = {
                "collected": collected,
                "user_id_hash": state.user_id_hash,
                "channel": str(state.channel),
                "language": state.language,
                "session_id": str(state.session_id),
            }
            input_text = render(input_template, ctx_data).strip()

            if not input_text:
                return _route_low("empty input after rendering")

            # Call LLM
            try:
                choice, confidence = await llm_adapter.generate_choice(
                    input_text=input_text,
                    candidates=candidates,
                    threshold=threshold,
                )
            except Exception:  # noqa: BLE001
                log.exception("llm_choose[%s]: LLM call failed", cfg["id"])
                return _route_low("LLM call failed")

            log.info(
                "llm_choose[%s]: input=%r → choice=%r confidence=%.2f threshold=%.2f",
                cfg["id"], input_text[:80], choice, confidence, threshold,
            )

            # Low confidence or invalid id → fallback
            if confidence < threshold or choice not in valid_ids:
                collected["_llm_choice"] = on_low_confidence
            else:
                collected["_llm_choice"] = choice

            collected["_llm_confidence"] = confidence
            collected["_llm_raw_choice"] = choice  # debug: keep the raw LLM pick

            return {
                "collected": collected,
                "current_node": cfg["id"],
                "llm_calls_this_session": state.llm_calls_this_session + 1,
            }

        return run

    def next_node(self, cfg: dict[str, Any]) -> str | None:
        # Routing is always conditional (via candidates + on_low_confidence).
        return None

    def register_conditional_edges(self, graph: Any, cfg: dict[str, Any]) -> bool:
        """Wire conditional edges: route to the chosen candidate id, or on_low_confidence."""
        candidates = cfg["candidates"]
        on_low_confidence = cfg["on_low_confidence"]
        node_id = cfg["id"]

        valid_ids = {c["id"] for c in candidates}
        all_targets = valid_ids | {on_low_confidence}

        def route(state: ConversationState) -> str:
            choice = (state.collected or {}).get("_llm_choice", on_low_confidence)
            # Defensively: only route to known targets
            if choice not in all_targets:
                log.warning("llm_choose route: unknown _llm_choice %r → on_low_confidence", choice)
                return on_low_confidence
            return choice

        graph.add_conditional_edges(node_id, route, {t: t for t in all_targets})
        return True
