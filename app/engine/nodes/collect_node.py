"""`collect` node — capture one or more field values.

YAML shape (single field):
    - id: collect_email
      type: collect
      prompt:
        text: "Please share your registered email."
      field:
        name: collected.email
        type: text          # text | select | number
      next: next_node_id

YAML shape (multiple fields in sequence):
    - id: collect_ticket_details
      type: collect
      prompts:
        - { field: collected.email,       text: "Your registered email:" }
        - { field: collected.description, text: "Brief description:", optional: true }
      next: raise_ticket

YAML shape (picker — dynamic options from API):
    - id: pick_course
      type: collect
      prompt:
        text: "Which course?"
      field:
        name: collected.course_id
        type: select
      dynamic_options:
        source: api
        integration: karmayogi
        request:
          method: GET
          url: "/api/courses/enrolled"
          params: { user_id: "{{ ctx.user_id_hash }}" }
        response_mapping:
          list_path: "$.courses"
          id_field: course_id
          label_field: title
          sub_label_field: completion_status
        search: { enabled: true }
        pagination: { enabled: true, page_size: 10 }
        cache_ttl: 300
      next: next_node_id
"""

from __future__ import annotations

import logging
from typing import Any, Callable

log = logging.getLogger(__name__)

from langgraph.graph import END

from langchain_core.messages import AIMessage

from app.engine.activity import Activity, PickerItem, QuickReply
from app.engine.nodes.base import NodeHandler
from app.engine.state import ConversationState, FlowStatus
from app.engine.template import render


class CollectNode(NodeHandler):
    node_type = "collect"

    def _validate(self, cfg: dict[str, Any]) -> None:
        super()._validate(cfg)
        if "prompt" not in cfg and "prompts" not in cfg:
            raise ValueError(f"collect node {cfg['id']!r} missing 'prompt' or 'prompts'")

    def next_node(self, cfg: dict[str, Any]) -> str | None:
        # Multi-field prompts: use conditional self-loop edges so each field
        # is collected one at a time before proceeding to next node.
        if cfg.get("prompts"):
            return None  # handled by register_conditional_edges()
        # dynamic_options with on_empty: needs conditional edges
        on_empty = cfg.get("on_empty") or (cfg.get("dynamic_options") or {}).get("on_empty")
        if on_empty:
            return None  # handled by register_conditional_edges()
        # Single-field or dynamic_options: simple static edge
        return cfg.get("next")

    def register_conditional_edges(self, graph: Any, cfg: dict[str, Any]) -> bool:
        """For multi-field sequential collect nodes: self-loop until all required
        fields are filled, then proceed to next node.

        This is how multi-turn field collection works without explicit interrupts:
        the graph stays on this node (interrupt_after fires each iteration) until
        all required fields have values in state.collected.
        """
        node_id = cfg["id"]
        next_target = cfg.get("next")
        on_empty = cfg.get("on_empty") or (cfg.get("dynamic_options") or {}).get("on_empty")
        on_other = cfg.get("on_other")

        if (on_empty or on_other) and cfg.get("dynamic_options"):
            field_cfg_r = cfg.get("field")
            field_key = field_cfg_r["name"].removeprefix("collected.") if field_cfg_r else None

            def route_picker(state: ConversationState) -> str:
                c = state.collected or {}
                if on_other and c.get("_other_requested"):
                    return on_other
                if field_key and c.get(field_key) is None:
                    return on_empty or next_target or node_id
                return next_target or node_id

            targets = set()
            if on_empty:
                targets.add(on_empty)
            if on_other:
                targets.add(on_other)
            if next_target:
                targets.add(next_target)
            targets.add(node_id)
            graph.add_conditional_edges(node_id, route_picker, {t: t for t in targets})
            return True

        prompts = cfg.get("prompts")
        if not prompts:
            return False  # single field or dynamic_options — plain next edge

        def route(state: ConversationState) -> str:
            collected = state.collected or {}
            for p in prompts:
                field_name = p["field"].removeprefix("collected.")
                # Loop back if a required field is unfilled
                if collected.get(field_name) is None and not p.get("optional"):
                    return node_id  # not done yet — self-loop
            return next_target or node_id  # all required fields filled

        targets = {node_id}
        if next_target:
            targets.add(next_target)
        graph.add_conditional_edges(node_id, route, {t: t for t in targets})
        return True

    def build(self, cfg: dict[str, Any]) -> Callable[[ConversationState], dict]:
        self._validate(cfg)

        # Multi-field sequential prompts
        prompts_seq: list[dict] | None = cfg.get("prompts")

        # Single-field prompt
        prompt_cfg: dict | None = cfg.get("prompt")
        field_cfg: dict | None = cfg.get("field")
        dynamic_options: dict | None = cfg.get("dynamic_options")

        async def run(state: ConversationState) -> dict:
            _picker_extras: dict[str, dict] = {}  # populated if dynamic_options resolves extra_fields
            ctx = _state_ctx(state)
            activities: list[dict] = []
            bot_prompt_text: str = ""  # tracked for state.messages

            if prompts_seq:
                # Sequential field capture — emit first unfilled prompt
                for p in prompts_seq:
                    field_name = p["field"]
                    if _path_get(state.collected, field_name.removeprefix("collected.")) is None:
                        text = render(p["text"], ctx)
                        bot_prompt_text = text
                        activities.append(Activity.markdown(text).model_dump(exclude_none=True))
                        activities.append(
                            Activity.input(
                                input_id=field_name,
                                placeholder=p.get("placeholder", ""),
                            ).model_dump(exclude_none=True)
                        )
                        break  # one field at a time
                else:
                    # All prompts filled — proceed (no activities emitted)
                    return {
                        "current_node": cfg["id"],
                        "status": FlowStatus.ACTIVE,
                    }
            elif dynamic_options:
                # Picker — resolve list from API
                text = render(prompt_cfg.get("text", "") if prompt_cfg else "", ctx)
                if text:
                    bot_prompt_text = text
                    activities.append(Activity.markdown(text).model_dump(exclude_none=True))

                items, extras_map = await _resolve_dynamic_options(
                    dynamic_options, ctx, services=self.services
                )
                placeholder = (
                    prompt_cfg.get("placeholder") if prompt_cfg else None
                ) or dynamic_options.get("search", {}).get("placeholder", "Search…")
                if not items:
                    empty_msg = dynamic_options.get("empty_message", "No results found.")
                    activities.append(Activity.markdown(empty_msg).model_dump(exclude_none=True))
                    on_empty_rt = cfg.get("on_empty") or dynamic_options.get("on_empty")
                    if on_empty_rt:
                        return {
                            "pending_activities": state.pending_activities + activities,
                            "current_node": cfg["id"],
                            "status": FlowStatus.ACTIVE,
                            "collected": {**state.collected,
                                          (field_cfg["name"].removeprefix("collected.") if field_cfg else "_empty"): None},
                        }
                other_opt_cfg = cfg.get("other_option")
                other_option = None
                if other_opt_cfg:
                    other_option = QuickReply(
                        id=other_opt_cfg["id"],
                        label=other_opt_cfg["label"],
                    )
                activities.append(
                    Activity.picker(
                        picker_id=field_cfg["name"] if field_cfg else cfg["id"],
                        items=items,
                        placeholder=placeholder,
                        other_option=other_option,
                    ).model_dump(exclude_none=True)
                )
                # Store per-item extras in collected so pick_item can merge them
                if extras_map:
                    _picker_extras = extras_map
            else:
                # Single-field text or select
                text = render(prompt_cfg.get("text", "") if prompt_cfg else "", ctx)
                if text:
                    bot_prompt_text = text
                    activities.append(Activity.markdown(text).model_dump(exclude_none=True))
                activities.append(
                    Activity.input(
                        input_id=field_cfg["name"] if field_cfg else "value",
                        placeholder=(prompt_cfg.get("placeholder", "") if prompt_cfg else ""),
                    ).model_dump(exclude_none=True)
                )

            # Add bot prompt to conversation history so the LLM transcript is complete.
            # (message_node does the same for its own prompts.)
            bot_messages = [AIMessage(content=bot_prompt_text)] if bot_prompt_text else []

            collected_update = {**state.collected}
            if _picker_extras:
                collected_update["_picker_item_extras"] = _picker_extras
            return {
                "pending_activities": state.pending_activities + activities,
                "current_node": cfg["id"],
                "status": FlowStatus.AWAITING_USER,
                "collected": collected_update,
                "messages": bot_messages,
            }

        return run


def _state_ctx(state: ConversationState) -> dict[str, Any]:
    return {
        "collected": state.collected,
        "counters": state.counters,
        "user_id_hash": state.user_id_hash,
        "channel": state.channel,
    }


def _path_get(d: dict[str, Any], path: str) -> Any:
    """Get nested dict value by dotted path. Returns None if missing."""
    parts = path.split(".")
    cur: Any = d
    for p in parts:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
        if cur is None:
            return None
    return cur


async def _resolve_dynamic_options(
    cfg: dict[str, Any],
    ctx: dict[str, Any],
    services: dict[str, Any],
) -> tuple[list[PickerItem], dict[str, dict]]:
    """Resolve a `dynamic_options:` block by calling the registered integration.

    Renders Jinja templates in the request block against ``ctx``, then delegates
    the HTTP call to the integration adapter (KarmayogiService, etc.).
    Response items are mapped via ``response_mapping`` to produce a list of
    PickerItem values for the picker activity.
    """
    from app.engine.template import render as _render, render_native

    source = cfg.get("source", "api")

    if source == "context":
        context_key = cfg.get("context_key", "").removeprefix("collected.")
        items_raw = (ctx.get("collected") or {}).get(context_key, []) or []
        mapping   = cfg.get("response_mapping", {})
        id_field    = mapping.get("id_field", "id")
        label_field = mapping.get("label_field", "name")
        extra_fields_cfg = mapping.get("extra_fields", [])
        items: list[PickerItem] = []
        extras_map: dict[str, dict] = {}
        for raw in items_raw:
            if not isinstance(raw, dict):
                continue
            item_id = raw.get(id_field)
            label   = raw.get(label_field)
            if item_id and label:
                items.append(PickerItem(id=str(item_id), label=str(label)))
                for ef in extra_fields_cfg:
                    ef_from, ef_to = ef.get("from", ""), ef.get("to", "")
                    if ef_from and ef_to and ef_from in raw:
                        extras_map.setdefault(str(item_id), {})[ef_to.removeprefix("collected.")] = raw[ef_from]
        return items, extras_map

    if source != "api":
        raise NotImplementedError(f"dynamic_options.source = {source!r} not supported yet")

    integration_name = cfg["integration"]
    integration = services.get(integration_name)
    if integration is None:
        return [], {}

    request_cfg: dict[str, Any] = cfg.get("request", {})
    mapping: dict[str, Any] = cfg.get("response_mapping", {})

    # Render Jinja templates in url / body / params
    rendered_url = _render(request_cfg.get("url", ""), ctx)
    rendered_body: dict[str, Any] | None = None
    if "body" in request_cfg:
        rendered_body = _render_nested(request_cfg["body"], ctx, render_native)
    rendered_params: dict[str, str] | None = None
    if "params" in request_cfg:
        rendered_params = {k: _render(str(v), ctx) for k, v in request_cfg["params"].items()}

    try:
        result = await integration.execute_request(
            method=request_cfg.get("method", "GET").upper(),
            url=rendered_url,
            params=rendered_params,
            body=rendered_body,
            headers={k: _render(str(v), ctx) for k, v in request_cfg.get("headers", {}).items()} if "headers" in request_cfg else None,
        )
    except Exception:  # noqa: BLE001
        return [], {}  # network failure → empty picker (user can still type or try again)

    # Navigate to the list in the response using list_path (e.g. "$.courses" → "courses")
    list_path = mapping.get("list_path", "").lstrip("$").lstrip(".")
    items_raw: list = result
    if list_path:
        for part in list_path.split("."):
            if isinstance(items_raw, dict):
                items_raw = items_raw.get(part, [])
            else:
                items_raw = []
                break

    if not isinstance(items_raw, list):
        return [], {}

    id_field    = mapping.get("id_field", "id")
    label_field = mapping.get("label_field", "name")
    meta_field  = mapping.get("sub_label_field", "")
    extra_fields: list[dict] = mapping.get("extra_fields", [])
    filter_items: dict | None = cfg.get("filter_items")

    # Import transforms registry so extra_fields can use named transforms
    from app.engine.nodes.api_call_node import _TRANSFORMS

    items: list[PickerItem] = []
    extras_map: dict[str, dict] = {}  # {item_id: {collected_key: value}}
    for raw in items_raw:
        if not isinstance(raw, dict):
            continue
        # Client-side item filter — applied before building the picker item.
        # Useful when the API ignores server-side filters (e.g. Karmayogi
        # enrollment list returns all enrolled courses regardless of status).
        # YAML shape:
        #   filter_items:
        #     field: completionPercentage   # field in the raw item
        #     operator: gt                  # gt | gte | lt | lte | eq | neq
        #     value: 0
        if filter_items:
            fi_field = filter_items.get("field", "")
            fi_op    = filter_items.get("operator", "gt")
            fi_val   = filter_items.get("value", 0)
            raw_val  = _item_get(raw, fi_field)
            try:
                raw_val = type(fi_val)(raw_val)  # coerce to same type as threshold
            except (TypeError, ValueError):
                raw_val = None
            include = False
            if raw_val is not None:
                if fi_op == "gt":       include = raw_val > fi_val
                elif fi_op == "gte":    include = raw_val >= fi_val
                elif fi_op == "lt":     include = raw_val < fi_val
                elif fi_op == "lte":    include = raw_val <= fi_val
                elif fi_op == "eq":     include = raw_val == fi_val
                elif fi_op == "neq":    include = raw_val != fi_val
                elif fi_op == "not_null": include = True  # raw_val already not None
            elif fi_op == "is_null":   include = True   # raw_val is None
            if not include:
                continue
        item_id = _item_get(raw, id_field)
        label   = _item_get(raw, label_field)
        if not item_id or not label:
            continue
        meta = str(raw[meta_field]) if meta_field and raw.get(meta_field) else None
        items.append(PickerItem(id=str(item_id), label=str(label), meta=meta))

        # Build extras entry for this item
        if extra_fields:
            item_extras: dict[str, Any] = {}
            for ef in extra_fields:
                src_key  = ef.get("from", "")
                dst_key  = ef.get("to", "").removeprefix("collected.")
                value    = _item_get(raw, src_key)
                transform_name = ef.get("transform")
                if transform_name and value is not None:
                    fn = _TRANSFORMS.get(transform_name)
                    if fn:
                        value = fn(value)
                log.info(
                    "[picker extras] item=%s  field=%s  raw=%r  stored=%r",
                    item_id, src_key, raw.get(src_key), value,
                )
                # Only write when the resolved value is non-None.
                # This allows multiple `from` sources to target the same `to`
                # field (e.g. `batchId` at top-level AND `batches[0].batchId`
                # as a fallback) without clobbering a previously-set valid
                # value with None when the secondary source is absent.
                if value is not None:
                    item_extras[dst_key] = value
            extras_map[str(item_id)] = item_extras

    return items, extras_map


def _render_nested(value: Any, ctx: dict[str, Any], render_fn: Any) -> Any:
    """Recursively render Jinja templates inside nested dicts/lists."""
    if isinstance(value, str):
        return render_fn(value, ctx)
    if isinstance(value, dict):
        return {k: _render_nested(v, ctx, render_fn) for k, v in value.items()}
    if isinstance(value, list):
        return [_render_nested(v, ctx, render_fn) for v in value]
    return value


def _item_get(raw: dict[str, Any], path: str) -> Any:
    """Get a value from a raw API item by dotted path.

    Supports both flat keys (e.g. 'courseId') and nested keys
    (e.g. 'event.name' → raw['event']['name']).
    Returns None if any level is missing.
    """
    cur: Any = raw
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
        if cur is None:
            return None
    return cur
