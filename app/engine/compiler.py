"""YAML → LangGraph compiler.

For each YAML flow, builds a `StateGraph[ConversationState]` and compiles it
with a checkpointer. Caches compiled graphs by flow_id.

CLI:
    python -m app.engine.compiler --validate flows/
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

from langgraph.graph import END, StateGraph
from ruamel.yaml import YAML

from app.engine.nodes import (
    DETERMINISTIC_NODE_TYPES,
    LLM_NODE_TYPES,
    NODE_HANDLERS,
)
from app.engine.state import ConversationState

yaml = YAML(typ="safe", pure=True)


def _get_interrupt_nodes(flow: dict[str, Any]) -> list[str]:
    """Return node IDs that should trigger LangGraph interrupt_after.

    Includes:
    - ``message`` nodes that have a ``quick_replies`` key
    - all ``collect`` nodes
    - ``resolution`` nodes that have a ``follow_up`` key
    - ``transfer_llm`` nodes without ``auto_raise: true`` (show confirmation, wait for user)
    """
    result: list[str] = []
    for node in flow.get("nodes", []):
        ntype = node.get("type")
        nid = node.get("id")
        if not nid:
            continue
        if ntype == "message" and "quick_replies" in node:
            result.append(nid)
        elif ntype == "collect":
            result.append(nid)
        elif ntype == "resolution" and "follow_up" in node:
            result.append(nid)
        elif ntype == "transfer_llm" and not node.get("auto_raise", False):
            result.append(nid)
    return result


class FlowCompilationError(Exception):
    """Raised when a YAML flow cannot be compiled."""


class FlowCompiler:
    """Compile YAML flow definitions into runnable LangGraph graphs."""

    def __init__(self, services: dict[str, Any]) -> None:
        self.services = services
        # Cache: flow_id → compiled graph
        self._graph_cache: dict[str, Any] = {}
        # Cache: flow_id → loaded YAML dict (for inspection/admin)
        self._flow_cache: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Loading + validation
    # ------------------------------------------------------------------

    def load_flow(self, yaml_path: Path) -> dict[str, Any]:
        """Load + parse a single YAML flow file. Raises FlowCompilationError."""
        try:
            with yaml_path.open(encoding="utf-8") as f:
                flow = yaml.load(f)
        except Exception as e:
            raise FlowCompilationError(f"{yaml_path}: YAML parse failed — {e}") from e

        self._resolve_imports(flow, yaml_path.parent)
        self._validate_flow(flow, yaml_path)
        return flow

    def _resolve_imports(self, flow: dict[str, Any], flows_dir: Path) -> dict[str, Any]:
        """Merge fragment nodes from ``imports:`` entries into the flow's node list.

        Each entry is either:
        - a plain string  → ``fragment_name``, no param substitution
        - a dict with ``fragment`` and optional ``with`` keys → param substitution

        Fragments are loaded from ``flows_dir/_shared/<fragment_name>.yaml``.
        Fragment nodes are appended *after* the flow's own nodes; if a node id
        already exists in the flow, the fragment node is skipped (flow wins).
        """
        imports = flow.get("imports")
        if not imports:
            return flow

        shared_dir = flows_dir / "_shared"
        # Build the set of node ids already declared in the flow
        existing_ids: set[str] = {n["id"] for n in flow.get("nodes", [])}

        for entry in imports:
            if isinstance(entry, str):
                fragment_name = entry
                params: dict[str, Any] = {}
            elif isinstance(entry, dict):
                fragment_name = entry["fragment"]
                params = entry.get("with", {}) or {}
            else:
                continue  # skip malformed entries

            frag_path = shared_dir / f"{fragment_name}.yaml"
            try:
                raw_text = frag_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                raise FlowCompilationError(
                    f"import fragment not found: {frag_path}"
                )
            except Exception as e:
                raise FlowCompilationError(
                    f"could not read fragment {frag_path}: {e}"
                ) from e

            # String substitution: {{ params.KEY }} → value
            # Also handles {{ params.KEY | default("fallback") }} — use provided value
            # when the key is present, or the fallback when it is absent.
            for key, val in params.items():
                raw_text = raw_text.replace(f"{{{{ params.{key} }}}}", str(val))
                raw_text = re.sub(
                    rf'\{{\{{\s*params\.{re.escape(key)}\s*\|\s*default\([^)]*\)\s*\}}\}}',
                    str(val),
                    raw_text,
                )
            # Replace any remaining {{ params.KEY | default("fallback") }} whose key
            # was NOT provided by the caller — substitute with the declared default.
            raw_text = re.sub(
                r'\{\{\s*params\.\w+\s*\|\s*default\((["\']?)([^)]*)\1\)\s*\}\}',
                lambda m: m.group(2),
                raw_text,
            )

            try:
                frag_data = yaml.load(raw_text)
            except Exception as e:
                raise FlowCompilationError(
                    f"fragment {frag_path}: YAML parse failed — {e}"
                ) from e

            frag_nodes = frag_data.get("nodes") or frag_data.get("shared_nodes") or []
            for frag_node in frag_nodes:
                node_id = frag_node.get("id")
                if node_id and node_id not in existing_ids:
                    flow.setdefault("nodes", []).append(frag_node)
                    existing_ids.add(node_id)

        return flow

    def _validate_flow(self, flow: dict[str, Any], yaml_path: Path) -> None:
        """Static validation of the YAML structure."""
        required = {"flow_id", "flow_type", "entry_node", "nodes"}
        missing = required - set(flow.keys())
        if missing:
            raise FlowCompilationError(f"{yaml_path}: missing required top-level keys: {missing}")

        node_ids = {n["id"] for n in flow["nodes"]}

        # Entry node must exist
        if flow["entry_node"] not in node_ids:
            raise FlowCompilationError(
                f"{yaml_path}: entry_node {flow['entry_node']!r} not found in nodes"
            )

        # Per-node validation + edge target check
        for node in flow["nodes"]:
            self._validate_node(node, node_ids, yaml_path)

        # CI rule: flow_type must be one of the 4 known values
        valid_types = {"deterministic", "deterministic_with_llm_fallback", "llm_guided_fsm", "llm_driven"}
        if flow["flow_type"] not in valid_types:
            raise FlowCompilationError(
                f"{yaml_path}: invalid flow_type {flow['flow_type']!r}; must be one of {valid_types}"
            )

        # CI rule: deterministic flows must not contain LLM node types
        if flow["flow_type"] == "deterministic":
            for node in flow["nodes"]:
                if node["type"] in LLM_NODE_TYPES:
                    raise FlowCompilationError(
                        f"{yaml_path}: deterministic flow has LLM node "
                        f"{node['id']!r} (type={node['type']!r}) — not allowed. "
                        f"Use flow_type=deterministic_with_llm_fallback if LLM is needed."
                    )

    def _validate_node(self, node: dict[str, Any], node_ids: set[str], yaml_path: Path) -> None:
        node_id = node.get("id")
        if not node_id:
            raise FlowCompilationError(f"{yaml_path}: node missing 'id'")

        node_type = node.get("type")
        if node_type not in NODE_HANDLERS:
            raise FlowCompilationError(
                f"{yaml_path}: node {node_id!r} has unknown type {node_type!r}; "
                f"valid types: {sorted(NODE_HANDLERS.keys())}"
            )

        # Edge target validation — every referenced next-node must exist
        edge_targets = _collect_edge_targets(node)
        for target in edge_targets:
            if target not in node_ids and target != "END":
                raise FlowCompilationError(
                    f"{yaml_path}: node {node_id!r} references unknown next-node {target!r}"
                )

    # ------------------------------------------------------------------
    # Compilation
    # ------------------------------------------------------------------

    def compile_flow(self, flow: dict[str, Any], checkpointer: Any = None) -> Any:
        """Build a runnable LangGraph from a parsed YAML flow."""
        flow_id = flow["flow_id"]
        cache_key = flow_id + (":cp" if checkpointer else "")

        if cache_key in self._graph_cache:
            return self._graph_cache[cache_key]

        graph = StateGraph(ConversationState)

        # Register every node
        for node_cfg in flow["nodes"]:
            handler_cls = NODE_HANDLERS[node_cfg["type"]]
            handler = handler_cls(self.services)
            node_fn = handler.build(node_cfg)
            graph.add_node(node_cfg["id"], node_fn)

        # Set entry point
        graph.set_entry_point(flow["entry_node"])

        # Wire edges
        for node_cfg in flow["nodes"]:
            handler_cls = NODE_HANDLERS[node_cfg["type"]]
            handler = handler_cls(self.services)

            # Conditional edges first (branch, etc.)
            if handler.register_conditional_edges(graph, node_cfg):
                continue

            # Static next-edge
            next_target = handler.next_node(node_cfg)
            if next_target is None:
                # Terminal (end node) or quick_replies / on_reply patterns
                # For now, handle on_reply by registering conditional edges based on the map
                on_reply = node_cfg.get("on_reply")
                if isinstance(on_reply, dict):
                    _wire_on_reply_edges(graph, node_cfg, on_reply)
                elif node_cfg["type"] == "end":
                    graph.add_edge(node_cfg["id"], END)
                # else: leave dangling — runtime will fail clearly
            else:
                if next_target == "END":
                    graph.add_edge(node_cfg["id"], END)
                else:
                    graph.add_edge(node_cfg["id"], next_target)

        interrupt_after = _get_interrupt_nodes(flow) if checkpointer else None
        compiled = graph.compile(
            checkpointer=checkpointer,
            **({"interrupt_after": interrupt_after} if interrupt_after else {}),
        )

        self._graph_cache[cache_key] = compiled
        self._flow_cache[flow_id] = flow
        return compiled

    def compile_directory(
        self, flows_dir: Path, checkpointer: Any = None
    ) -> dict[str, Any]:
        """Load + compile all `*.yaml` files in a directory. Returns flow_id → graph.

        Individual flow failures are logged and skipped so that one broken YAML
        never prevents other flows from loading (fail-open per flow).
        """
        import logging as _logging
        _log = _logging.getLogger(__name__)
        compiled: dict[str, Any] = {}
        for yaml_file in sorted(flows_dir.glob("*.yaml")):
            try:
                flow = self.load_flow(yaml_file)
                compiled[flow["flow_id"]] = self.compile_flow(flow, checkpointer)
            except Exception as exc:  # noqa: BLE001
                _log.error("⚠️  Skipping flow %s — compilation error: %s", yaml_file.name, exc)
        return compiled

    def get_flow(self, flow_id: str) -> dict[str, Any] | None:
        return self._flow_cache.get(flow_id)

    def is_flow_enabled(self, flow_id: str) -> bool:
        """Return True if the flow exists and is enabled (default: True).

        A flow with ``metadata.enabled: false`` is compiled and cached for
        validation purposes but will be refused by the API router. Omitting
        the field is the same as ``enabled: true``.
        """
        flow = self._flow_cache.get(flow_id)
        if flow is None:
            return False
        meta = flow.get("metadata") or {}
        return bool(meta.get("enabled", True))

    def get_menu_items(self) -> list[dict[str, Any]]:
        """Return menu items derived from loaded flow metadata.

        Each item: ``{flow_id, menu_label, menu_group, menu_group_order, menu_order}``.
        Flows are excluded when any of the following apply:
        - No ``metadata.menu_label`` set
        - ``metadata.menu_hidden: true``   (hides from menu; API still accessible)
        - ``metadata.enabled: false``      (disabled entirely; also blocked at API)

        Sorted by ``menu_order`` (then ``flow_id`` as tiebreaker).
        """
        items = []
        for flow_id, flow in self._flow_cache.items():
            meta = flow.get("metadata") or {}
            label = meta.get("menu_label")
            if not label:
                continue
            if meta.get("menu_hidden"):
                continue
            if not meta.get("enabled", True):
                continue
            items.append({
                "flow_id":         flow_id,
                "menu_label":      label,
                "menu_group":      meta.get("menu_group", "General"),
                "menu_group_order": int(meta.get("menu_group_order", 99)),
                "menu_order":      int(meta.get("menu_order", 99)),
            })
        return sorted(items, key=lambda x: (x["menu_order"], x["flow_id"]))

    def get_categories(self) -> list[str]:
        """Return ordered distinct category names that have at least one visible flow.

        Order is determined by the minimum ``menu_group_order`` across flows in
        each category, so the order is fully driven by YAML metadata — no
        hardcoded list in Python.
        """
        items = self.get_menu_items()
        group_min_order: dict[str, int] = {}
        for item in items:
            grp = item["menu_group"]
            gord = item["menu_group_order"]
            if grp not in group_min_order or gord < group_min_order[grp]:
                group_min_order[grp] = gord
        return sorted(group_min_order, key=lambda g: (group_min_order[g], g))

    def get_flows_for_category(self, category: str) -> list[dict[str, Any]]:
        """Return visible menu items for a single category, sorted by menu_order."""
        return [i for i in self.get_menu_items() if i["menu_group"] == category]


def _collect_edge_targets(node: dict[str, Any]) -> set[str]:
    """Extract all node-id references from a node config."""
    targets: set[str] = set()
    if "next" in node:
        targets.add(node["next"])
    if "on_success" in node:
        targets.add(node["on_success"])
    if "on_complete" in node:
        targets.add(node["on_complete"])
    for key in ("on_error",):
        if key in node:
            for v in (node[key] or {}).values():
                if isinstance(v, str):
                    targets.add(v)
    if "default" in node:
        targets.add(node["default"])
    for rule in node.get("rules", []):
        if isinstance(rule, dict) and "then" in rule:
            targets.add(rule["then"])
    # on_reply: collect choice-to-node values, but SKIP control keys (save_to, next)
    # to avoid treating field paths like "collected.sub_scenario" as node IDs.
    on_reply = node.get("on_reply")
    if isinstance(on_reply, dict):
        for k, v in on_reply.items():
            if k not in {"save_to", "next"} and isinstance(v, str):
                targets.add(v)
        if "next" in on_reply and isinstance(on_reply["next"], str):
            targets.add(on_reply["next"])
    if "on_low_confidence" in node:
        targets.add(node["on_low_confidence"])
    if "candidates" in node:
        for c in node["candidates"]:
            if isinstance(c, dict) and "id" in c:
                targets.add(c["id"])  # candidate ids ARE node ids in llm_choose
    return targets


def _wire_on_reply_edges(graph: Any, node_cfg: dict[str, Any], on_reply: dict[str, Any]) -> None:
    """Register conditional edges based on `on_reply` map.

    For message + resolution nodes with quick_replies, the channel sends back
    a `choice_id`; we route based on collected._last_choice_id.
    """
    # Extract the next-id map (excluding control keys like save_to, next)
    choice_to_node = {
        k: v for k, v in on_reply.items()
        if isinstance(v, str) and k not in {"save_to", "next"}
    }
    if not choice_to_node:
        # If just save_to + next, treat as simple next edge
        if "next" in on_reply and isinstance(on_reply["next"], str):
            graph.add_edge(node_cfg["id"], on_reply["next"])
        return

    def route(state: ConversationState) -> str:
        choice_id = (state.collected or {}).get("_last_choice_id")
        return choice_to_node.get(choice_id, next(iter(choice_to_node.values())))

    graph.add_conditional_edges(
        node_cfg["id"], route, {v: v for v in choice_to_node.values()}
    )


# ------------------------------------------------------------------
# CLI: python -m app.engine.compiler --validate flows/
# ------------------------------------------------------------------

def _cli() -> None:
    parser = argparse.ArgumentParser(description="iGOT Deterministic Chatbot flow compiler / validator")
    parser.add_argument("--validate", type=Path, help="Validate all YAML in directory")
    args = parser.parse_args()

    if args.validate:
        flows_dir: Path = args.validate
        if not flows_dir.is_dir():
            print(f"❌ Not a directory: {flows_dir}", file=sys.stderr)
            sys.exit(1)

        compiler = FlowCompiler(services={})
        errors: list[str] = []
        ok: list[str] = []
        for yaml_file in sorted(flows_dir.rglob("*.yaml")):
            # Skip fragment files inside _shared/ subdirectories — they are not
            # standalone flows and lack flow_id / entry_node / etc.
            if "_shared" in yaml_file.parts:
                continue
            # Skip on_hold/ subdirectory — flows moved there are not active
            if "on_hold" in yaml_file.parts:
                continue
            try:
                flow = compiler.load_flow(yaml_file)
                ok.append(f"✅ {yaml_file.name} ({flow['flow_id']}, {flow['flow_type']}, {len(flow['nodes'])} nodes)")
            except FlowCompilationError as e:
                errors.append(f"❌ {e}")

        print("\n".join(ok))
        if errors:
            print("\n".join(errors), file=sys.stderr)
            sys.exit(2)
        print(f"\n{len(ok)} flow(s) validated successfully.")
        return

    parser.print_help()


if __name__ == "__main__":
    _cli()
