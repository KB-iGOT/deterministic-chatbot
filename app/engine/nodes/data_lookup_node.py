"""`data_lookup` node — query an in-memory data service (no HTTP round-trip).

Used for lookups backed by local data sources (e.g. the YP allocation Excel
sheet) that are loaded once at startup.

YAML shape:

    - id: lookup_yp_contact
      type: data_lookup
      service: yp_lookup                      # key in ServiceRegistry
      key: "{{ ctx.collected.org_channel }}"  # Jinja2 expression → lookup key
      response_mapping:
        - { from: name,   to: collected.yp_name }
        - { from: email,  to: collected.yp_email }
        - { from: mobile, to: collected.yp_mobile }
      on_success: branch_yp_found             # routed when result is not None
      on_error:
        any: show_static_yp_fallback          # routed when result is None or service missing

``response_mapping`` entries use plain field names (not JSONPath) to read from
the flat dict returned by the service's ``lookup()`` method.

Routing follows the same convention as ``api_call``:
  - ``on_success`` is the default next edge (no error written to collected).
  - ``on_error.any`` is reached when the lookup returns None or the service
    is unavailable; ``_last_api_error`` is written to ``collected`` so the
    conditional-edge router can dispatch correctly.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

log = logging.getLogger(__name__)

from app.engine.nodes.base import NodeHandler
from app.engine.state import ConversationState
from app.engine.template import render


class DataLookupNode(NodeHandler):
    node_type = "data_lookup"

    def _validate(self, cfg: dict[str, Any]) -> None:
        super()._validate(cfg)
        for field in ("service", "key", "on_success"):
            if field not in cfg:
                raise ValueError(
                    f"data_lookup node {cfg['id']!r} requires '{field}'"
                )

    def build(self, cfg: dict[str, Any]) -> Callable[[ConversationState], dict]:
        self._validate(cfg)

        svc_name: str       = cfg["service"]
        key_tmpl: str       = cfg["key"]
        response_mapping: list = cfg.get("response_mapping", [])

        services = self.services  # captured from constructor

        async def run(state: ConversationState) -> dict:
            node_id = cfg["id"]

            ctx = {
                "collected": state.collected,
                "counters":  state.counters,
                "user_id_hash": state.user_id_hash,
                "channel":   state.channel,
            }

            # Render lookup key
            try:
                key_value = render(key_tmpl, ctx)
            except Exception as exc:
                log.warning("[data_lookup] node=%s key render failed: %s", node_id, exc)
                return _record_error(state, cfg, f"render_failed:{exc}")

            # Retrieve service
            svc = services.get(svc_name)
            if svc is None:
                log.error(
                    "[data_lookup] node=%s service %r not in registry", node_id, svc_name
                )
                return _record_error(state, cfg, "service_not_found")

            # Call lookup
            try:
                result: dict | None = svc.lookup(key_value)
            except Exception as exc:
                log.exception("[data_lookup] node=%s lookup() raised: %s", node_id, exc)
                return _record_error(state, cfg, f"lookup_error:{exc}")

            if result is None:
                log.debug("[data_lookup] node=%s no entry for key=%r", node_id, key_value)
                return _record_error(state, cfg, "not_found")

            # Apply response mapping → build collected updates
            new_collected = dict(state.collected)
            for entry in response_mapping:
                field_name = entry.get("from")
                target     = entry.get("to", "")
                if field_name and field_name in result:
                    key = target.removeprefix("collected.")
                    new_collected[key] = result[field_name]

            # Clear any previous error so on_success edge fires
            new_collected.pop("_last_api_error", None)

            log.debug(
                "[data_lookup] node=%s found entry for key=%r", node_id, key_value
            )
            return {
                "current_node": node_id,
                "collected": new_collected,
            }

        return run

    def next_node(self, cfg: dict[str, Any]) -> str | None:
        return cfg.get("on_success")

    def register_conditional_edges(self, graph: Any, cfg: dict[str, Any]) -> bool:
        """Register conditional routing when ``on_error`` is declared."""
        on_error = cfg.get("on_error")
        if not isinstance(on_error, dict):
            return False

        on_success = cfg["on_success"]
        node_id    = cfg["id"]

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
        "[data_lookup] node=%s recording error → %r (will route via on_error)",
        cfg["id"], error,
    )
    return {
        "current_node": cfg["id"],
        "collected": {**state.collected, "_last_api_error": error},
    }
