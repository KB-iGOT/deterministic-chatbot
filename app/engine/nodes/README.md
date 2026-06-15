# Engine Nodes

> **Flow authors: you don't need to read this folder.** YAML node types are documented in [`flows/README.md`](../../../flows/README.md).

This folder has one Python file per YAML `type:` keyword. Each file translates a YAML node definition into a LangGraph function that runs at conversation time.

---

## What each file does

| File | YAML `type:` | What it does at runtime |
|---|---|---|
| `message_node.py` | `message` | Renders bot text via Jinja, emits `quick_replies` activity if present, then pauses for user input |
| `collect_node.py` | `collect` | Shows a prompt, optionally fetches dynamic options from API (picker), pauses for user input. Caches per-item extra fields so a second API call is not needed after selection |
| `branch_node.py` | `branch` | Evaluates `if/then` rules using `simpleeval` (sandboxed Python expressions), routes to the matching next node. No user interaction. |
| `api_call_node.py` | `api_call` | Renders the `request:` block via Jinja, calls the registered integration adapter (Karmayogi/Zoho), applies `response_mapping` (JSONPath + transforms + find_where + sub_path), updates `collected`. Contains the `_TRANSFORMS` registry. |
| `resolution_node.py` | `resolution` | Emits numbered steps followed by a satisfaction yes/no question |
| `end_node.py` | `end` | Terminates the LangGraph (emits END) with `self_served` or `ticket_raised` outcome |
| `increment_and_branch_node.py` | `increment_and_branch` | Increments a named counter and routes based on the new value (used in Mode B to decide when to stop retrying and call the LLM) |
| `transfer_llm_node.py` | `transfer_llm` | PII-redacts conversation context, calls Vertex AI Gemini, returns AI-generated response or falls back to a ticket template (Mode B only) |
| `llm_choose_node.py` | `llm_choose` | Mode C (Phase 2) — LLM picks one of N candidate node IDs |
| `open_llm_subgraph_node.py` | `open_llm_subgraph` | Mode D (Phase 2) — full agentic LangGraph subgraph |
| `base.py` | — | `NodeHandler` base class — subclass this to add a new node type |
| `__init__.py` | — | `NODE_HANDLERS` registry — maps `type:` string → handler class |

---

## Why `api_call_node.py` is the largest file

It does four things that can't be simplified further:

1. **Jinja rendering** — `url`, `params`, `body`, `headers` are all Jinja templates resolved against the current conversation state
2. **JSONPath parsing** — `response_mapping` extracts fields from arbitrary nested API responses using dotted paths + wildcards
3. **Data transforms** — 5 named transforms handle Karmayogi-specific data formats (Unix timestamps, status strings, nested content status dicts). These live here so YAML can reference them by name without Python
4. **Error routing** — `on_error` conditions (timeout / not_found / any) are wired as LangGraph conditional edges

All of this is declared in YAML. The Python is only the interpreter.

---

## Adding a new transform (most common reason to edit this folder)

If a new Karmayogi API returns data in a format not handled by the existing 5 transforms, add to `api_call_node.py`:

```python
def _my_transform(value: Any) -> Any:
    if value is None:
        return <safe default>
    # convert
    ...

_TRANSFORMS["my_transform"] = _my_transform
```

Then use in YAML:
```yaml
response_mapping:
  - { from: $.someField, to: collected.result, transform: my_transform }
```

No other Python changes needed.
