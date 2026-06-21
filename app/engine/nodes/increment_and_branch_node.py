"""`increment_and_branch` node — increments a named counter then branches on the result.

Used for deterministic loops, e.g. iterating through a list of Program child
course IDs to call the Admin Content State API once per course.

YAML shape:
    - id: loop_child_course
      type: increment_and_branch
      counter: child_loop_idx
      rules:
        - { if: "ctx.counters.child_loop_idx < len(ctx.collected.child_course_ids)",
            then: api_admin_content_state_loop }
      default: branch_on_technical_issue

Semantics:
    1. Increment ``state.counters[counter]`` by 1.
    2. Evaluate each rule's ``if`` expression against the updated state.
    3. Route to the first matching rule's ``then``, or ``default`` if none match.

Counter starts at 0 (default for any unseen key in ``state.counters``), so the
first api_call node before this node executes with index 0, after increment the
counter becomes 1 and the next iteration uses index 1, and so on.
"""

from __future__ import annotations

from typing import Any, Callable

from app.engine.expression import ExpressionEvaluator
from app.engine.nodes.base import NodeHandler
from app.engine.state import ConversationState


class IncrementAndBranchNode(NodeHandler):
    node_type = "increment_and_branch"

    def __init__(self, services: dict[str, Any]) -> None:
        super().__init__(services)
        self._evaluator = ExpressionEvaluator()

    def _validate(self, cfg: dict[str, Any]) -> None:
        super()._validate(cfg)
        if "counter" not in cfg:
            raise ValueError(
                f"increment_and_branch node {cfg['id']!r} requires 'counter'"
            )
        if "rules" not in cfg or not isinstance(cfg["rules"], list):
            raise ValueError(
                f"increment_and_branch node {cfg['id']!r} requires non-empty 'rules' list"
            )
        if "default" not in cfg:
            raise ValueError(
                f"increment_and_branch node {cfg['id']!r} requires 'default'"
            )

    def build(self, cfg: dict[str, Any]) -> Callable[[ConversationState], dict]:
        self._validate(cfg)
        counter_name = cfg["counter"]

        def run(state: ConversationState) -> dict:
            current = state.counters.get(counter_name, 0)
            new_val = current + 1
            return {
                "current_node": cfg["id"],
                "counters": {**state.counters, counter_name: new_val},
            }

        return run

    def next_node(self, cfg: dict[str, Any]) -> str | None:
        return None  # routing handled via conditional edges

    def register_conditional_edges(self, graph: Any, cfg: dict[str, Any]) -> bool:
        """Add conditional edges based on post-increment counter value."""
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

        targets = {r["then"] for r in rules} | {default_next}
        graph.add_conditional_edges(node_id, route, {t: t for t in targets})
        return True
