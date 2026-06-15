"""`branch` node — conditional next-node based on context expressions.

No UI emitted; pure routing.

YAML shape:
    - id: branch_on_status
      type: branch
      rules:
        - { if: "ctx.collected.cert_status == 'generated'", then: cert_ready }
        - { if: "ctx.collected.cert_status == 'pending'",   then: cert_pending }
      default: cert_not_eligible
"""

from __future__ import annotations

from typing import Any, Callable

from app.engine.expression import ExpressionEvaluator
from app.engine.nodes.base import NodeHandler
from app.engine.state import ConversationState


class BranchNode(NodeHandler):
    node_type = "branch"

    def __init__(self, services: dict[str, Any]) -> None:
        super().__init__(services)
        self._evaluator = ExpressionEvaluator()

    def _validate(self, cfg: dict[str, Any]) -> None:
        super()._validate(cfg)
        if "rules" not in cfg or not isinstance(cfg["rules"], list):
            raise ValueError(f"branch node {cfg['id']!r} requires non-empty 'rules' list")
        if "default" not in cfg:
            raise ValueError(f"branch node {cfg['id']!r} requires 'default'")

    def build(self, cfg: dict[str, Any]) -> Callable[[ConversationState], dict]:
        self._validate(cfg)

        rules = cfg["rules"]
        default_next = cfg["default"]

        def run(state: ConversationState) -> dict:
            # Branch only sets current_node; routing happens in conditional edges.
            return {"current_node": cfg["id"]}

        return run

    def next_node(self, cfg: dict[str, Any]) -> str | None:
        return None  # conditional

    def register_conditional_edges(self, graph: Any, cfg: dict[str, Any]) -> bool:
        """Add conditional edges to the LangGraph based on `rules`."""
        rules = cfg["rules"]
        default_next = cfg["default"]
        node_id = cfg["id"]
        evaluator = self._evaluator

        def route(state: ConversationState) -> str:
            ctx = {
                "collected": state.collected,
                "counters": state.counters,
                "user_id_hash": state.user_id_hash,
                "channel": state.channel,
            }
            for rule in rules:
                try:
                    if evaluator.evaluate(rule["if"], ctx):
                        return rule["then"]
                except Exception:  # noqa: BLE001
                    continue
            return default_next

        # Mapping: every possible next-node id → itself, plus default
        targets = {r["then"] for r in rules} | {default_next}
        graph.add_conditional_edges(node_id, route, {t: t for t in targets})
        return True
