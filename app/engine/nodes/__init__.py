"""Node handlers — one per YAML node type.

Each handler is a class with:
  - `node_type` class var matching the YAML `type:` field
  - `build(yaml_config) -> Callable` that returns a LangGraph node function
  - `register_edges(graph, yaml_config)` that wires up next/branch edges

CI rule: deterministic node types (Mode A) must NEVER call the LLM adapter.
Only `transfer_llm`, `llm_choose`, `open_llm_subgraph` may.
"""

from app.engine.nodes.api_call_node import ApiCallNode
from app.engine.nodes.base import NodeHandler
from app.engine.nodes.branch_node import BranchNode
from app.engine.nodes.data_lookup_node import DataLookupNode
from app.engine.nodes.collect_node import CollectNode
from app.engine.nodes.end_node import EndNode
from app.engine.nodes.increment_and_branch_node import IncrementAndBranchNode
from app.engine.nodes.llm_choose_node import LLMChooseNode
from app.engine.nodes.message_node import MessageNode
from app.engine.nodes.open_llm_subgraph_node import OpenLLMSubgraphNode
from app.engine.nodes.resolution_node import ResolutionNode
from app.engine.nodes.transfer_llm_node import TransferLLMNode
from app.engine.nodes.engineering_ticket_node import EngineeringTicketNode

# Registry — keyed by YAML `type:` value
NODE_HANDLERS: dict[str, type[NodeHandler]] = {
    "message": MessageNode,
    "collect": CollectNode,
    "branch": BranchNode,
    "api_call": ApiCallNode,
    "data_lookup": DataLookupNode,
    "increment_and_branch": IncrementAndBranchNode,
    "resolution": ResolutionNode,
    "transfer_llm": TransferLLMNode,
    "llm_choose": LLMChooseNode,
    "open_llm_subgraph": OpenLLMSubgraphNode,
    "end": EndNode,
    "engineering_ticket": EngineeringTicketNode,
}

# Deterministic node types — must not call LLM. Enforced in CI.
DETERMINISTIC_NODE_TYPES = frozenset(
    {"message", "collect", "branch", "api_call", "data_lookup", "increment_and_branch", "resolution", "end", "engineering_ticket"}
)

LLM_NODE_TYPES = frozenset({"transfer_llm", "llm_choose", "open_llm_subgraph"})

__all__ = ["NODE_HANDLERS", "DETERMINISTIC_NODE_TYPES", "LLM_NODE_TYPES", "NodeHandler"]
