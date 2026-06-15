"""Base class + protocol for node handlers."""

from __future__ import annotations

from typing import Any, Callable, ClassVar

from app.engine.state import ConversationState


class NodeHandler:
    """Base for all node-type handlers.

    Subclasses override:
      - `node_type` class var (e.g. "message")
      - `_validate(cfg)` to check the YAML config shape
      - `build(cfg)` to return a LangGraph node function (sync or async)
      - `next_node(cfg)` to return the static next-node id, or None if conditional

    For conditional routing, subclasses also override `register_edges()`.
    """

    node_type: ClassVar[str] = "base"

    def __init__(self, services: dict[str, Any]) -> None:
        self.services = services  # ServiceRegistry — has karmayogi, zoho, llm, etc.

    def _validate(self, cfg: dict[str, Any]) -> None:
        """Override to assert required keys. Raise ValueError if invalid."""
        if "id" not in cfg:
            raise ValueError(f"{self.node_type} node missing required 'id' field")

    def build(self, cfg: dict[str, Any]) -> Callable[[ConversationState], Any]:
        """Return a LangGraph node function: (state) -> dict (state updates)."""
        raise NotImplementedError

    def next_node(self, cfg: dict[str, Any]) -> str | None:
        """Return the static next-node id for a simple edge.

        Return None if this node has conditional edges (override `register_edges`).
        """
        return cfg.get("next")

    def register_conditional_edges(
        self,
        graph: Any,  # langgraph.graph.StateGraph
        cfg: dict[str, Any],
    ) -> bool:
        """Override to add conditional edges directly to the graph.

        Returns True if conditional edges were added (caller skips default edge).
        Returns False if this node uses simple next-edge (caller adds it).
        """
        return False
