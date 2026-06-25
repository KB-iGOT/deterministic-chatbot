"""`api_call` node — invoke an external API with an EXPLICIT request block.

Design principle: API details (method, URL, params, body, response mapping) live
in the YAML, not in Python code. This keeps flows readable by non-developers —
they can see exactly which API the bot calls and what data flows in/out.

Python "integration adapters" (KarmayogiService, ZohoDeskAdapter, etc.) provide
ONLY:
  - Base URL (so YAML uses relative paths)
  - Auth header injection (so YAML never contains secrets)
  - OAuth refresh logic (e.g. Zoho)
  - Common retry policy
  - Optional response unwrapping (e.g. Karmayogi wraps responses in {result: ...})

YAML shape (canonical example):

    - id: fetch_user
      type: api_call
      integration: karmayogi              # which adapter executes the request
      request:
        method: GET                       # GET | POST | PUT | PATCH | DELETE
        url: "/api/user/private/v1/read/{{ ctx.user_id_hash }}"
        params:                           # query string
          fields: "firstName,lastName,profileDetails"
        body:                             # JSON body for POST/PUT/PATCH
          filters:
            limit: 10
        headers:                          # extra headers (auth handled by adapter)
          X-Channel: "{{ ctx.channel }}"
      response_mapping:                   # JSONPath → context dotted-path
        - { from: $.result.firstName,         to: collected.first_name }
        - { from: $.result.profileDetails,    to: collected.profile_details }
        - { from: $.result.enrollments,       to: collected.enrollments }
      on_success: branch_on_user
      on_error:
        timeout:    escalate_timeout
        not_found:  user_not_found
        any:        escalate_generic
      timeout_ms: 5000
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
from typing import Any, Callable

log = logging.getLogger(__name__)

# Module-level in-memory TTL cache for api_call nodes that declare `cache:`.
# Key: MD5 of (integration, method, url, params, body).
# Value: (expiry_timestamp, result).
# Shared across all sessions/requests; cleared on server restart.
_NODE_RESPONSE_CACHE: dict[str, tuple[float, Any]] = {}

from app.engine.nodes.base import NodeHandler
from app.engine.state import ConversationState
from app.engine.template import render


def _build_env_vars() -> dict[str, str]:
    """Build safe, non-secret env vars exposed to YAML templates as ``{{ env.VAR }}``.

    Only explicit non-sensitive config values are included — secrets (tokens,
    passwords, keys) are never exposed to templates.
    """
    from app.config import settings
    return {
        "ZOHO_DEPARTMENT_ID": settings.zoho_department_id,
    }


class ApiCallNode(NodeHandler):
    node_type = "api_call"

    def _validate(self, cfg: dict[str, Any]) -> None:
        super()._validate(cfg)
        if "integration" not in cfg:
            raise ValueError(
                f"api_call node {cfg['id']!r} requires 'integration' (e.g. karmayogi, zoho_desk_api)"
            )
        if "request" not in cfg or not isinstance(cfg["request"], dict):
            raise ValueError(
                f"api_call node {cfg['id']!r} requires a 'request' block "
                f"with method + url"
            )
        req = cfg["request"]
        if "method" not in req or "url" not in req:
            raise ValueError(
                f"api_call node {cfg['id']!r} 'request' must specify 'method' and 'url'"
            )
        if req["method"].upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise ValueError(
                f"api_call node {cfg['id']!r} unsupported method {req['method']!r}"
            )
        if "on_success" not in cfg:
            raise ValueError(f"api_call node {cfg['id']!r} requires 'on_success'")

    def build(self, cfg: dict[str, Any]) -> Callable[[ConversationState], dict]:
        self._validate(cfg)

        integration_name: str = cfg["integration"]
        request_cfg: dict = cfg["request"]
        response_mapping: list = cfg.get("response_mapping", [])
        timeout_ms: int = cfg.get("timeout_ms", 10_000)

        async def run(state: ConversationState) -> dict:
            node_id = cfg["id"]
            integration = self.services.get(integration_name)
            if integration is None:
                log.error(
                    "[api_call] node=%s integration=%r not registered in ServiceRegistry — routing to on_error",
                    node_id, integration_name,
                )
                return _record_error(state, cfg, "integration_not_registered")

            ctx = _state_ctx(state)
            env_vars = _build_env_vars()

            # Render every part of the request via Jinja against ctx + env
            try:
                rendered = _render_request(request_cfg, ctx, env_vars)
            except Exception as e:  # noqa: BLE001
                log.error(
                    "[api_call] node=%s request template render failed: %s\n  request_cfg=%s",
                    node_id, e, request_cfg,
                )
                return _record_error(state, cfg, f"request_render_failed:{e}")

            # Replace __SESSION_TOKEN__ sentinel with the actual Keycloak JWT from state.
            # The token is never exposed to Jinja templates — only YAML headers that
            # explicitly declare this sentinel value receive the user JWT.
            if rendered.get("headers"):
                _tok = state.session_token
                rendered["headers"] = {
                    k: (_tok if v == "__SESSION_TOKEN__" else v)
                    for k, v in rendered["headers"].items()
                }

            log.info(
                "[api_call] node=%s  integration=%s  %s %s  timeout=%dms",
                node_id, integration_name, rendered["method"], rendered["url"], timeout_ms,
            )

            # Cache check — skip HTTP call if a valid cached result exists.
            cache_cfg = cfg.get("cache")
            cache_key: str | None = None
            cache_ttl: int = 3600
            result: Any = None
            cache_hit = False

            if cache_cfg:
                cache_ttl = int(cache_cfg.get("ttl_seconds", 3600))
                _raw = json.dumps({
                    "i": integration_name,
                    "m": rendered["method"],
                    "u": rendered["url"],
                    "p": rendered.get("params"),
                    "b": rendered.get("body"),
                }, sort_keys=True, default=str)
                cache_key = hashlib.md5(_raw.encode()).hexdigest()  # noqa: S324
                cached = _NODE_RESPONSE_CACHE.get(cache_key)
                if cached:
                    expiry, cached_result = cached
                    if time.time() < expiry:
                        log.info("[api_call] node=%s cache HIT (key=%.8s ttl=%ds)", node_id, cache_key, cache_ttl)
                        result = cached_result
                        cache_hit = True

            if not cache_hit:
                # Delegate the HTTP call to the integration adapter.
                try:
                    result = await asyncio.wait_for(
                        integration.execute_request(
                            method=rendered["method"],
                            url=rendered["url"],
                            params=rendered.get("params"),
                            body=rendered.get("body"),
                            headers=rendered.get("headers"),
                        ),
                        timeout=timeout_ms / 1000.0,
                    )
                except asyncio.TimeoutError:
                    log.error(
                        "[api_call] node=%s timed out after %dms (integration=%s %s %s)",
                        node_id, timeout_ms, integration_name, rendered["method"], rendered["url"],
                    )
                    return _record_error(state, cfg, "timeout")
                except IntegrationNotFound:
                    log.error(
                        "[api_call] node=%s got 404 Not Found from %s %s %s",
                        node_id, integration_name, rendered["method"], rendered["url"],
                    )
                    return _record_error(state, cfg, "not_found")
                except Exception as e:  # noqa: BLE001
                    log.error(
                        "[api_call] node=%s raised %s: %s  (integration=%s %s %s)",
                        node_id, type(e).__name__, e,
                        integration_name, rendered["method"], rendered["url"],
                    )
                    return _record_error(state, cfg, f"any:{e}")

                log.info("[api_call] node=%s completed successfully", node_id)

            # Pagination: if `paginate` is configured, fetch remaining pages and
            # merge all items into `result` before response_mapping runs.
            # Skipped on cache hit — the cached result already has all pages merged.
            paginate_cfg = cfg.get("paginate") if not cache_hit else None
            if paginate_cfg:
                p_count_path = paginate_cfg.get("total_count_path", "").lstrip("$").lstrip(".")
                p_list_path  = paginate_cfg.get("list_path", "").lstrip("$").lstrip(".")
                p_page_param = paginate_cfg.get("page_param", "pageNumber")
                p_page_size  = paginate_cfg.get("page_size", 100)
                max_pages    = int(paginate_cfg.get("max_pages", 50))
                if not p_page_size:
                    log.warning("[api_call] node=%s page_size is 0 — defaulting to 100", node_id)
                    p_page_size = 100
                total_count  = _jsonpath_get(result, p_count_path) or 0
                total_pages  = min(math.ceil(total_count / p_page_size), max_pages)
                all_items: list = list(_jsonpath_get(result, p_list_path) or [])
                for page_num in range(2, total_pages + 1):
                    page_body = {**(rendered.get("body") or {}), p_page_param: page_num}
                    try:
                        page_result = await asyncio.wait_for(
                            integration.execute_request(
                                method=rendered["method"],
                                url=rendered["url"],
                                params=rendered.get("params"),
                                body=page_body,
                                headers=rendered.get("headers"),
                            ),
                            timeout=timeout_ms / 1000.0,
                        )
                        page_items = _jsonpath_get(page_result, p_list_path) or []
                        all_items.extend(page_items)
                        log.info("[api_call] node=%s page=%d fetched %d items (total so far: %d)",
                                 node_id, page_num, len(page_items), len(all_items))
                    except Exception:  # noqa: BLE001
                        log.warning("[api_call] node=%s pagination stopped at page %d", node_id, page_num)
                        break
                # Write merged list back into result so response_mapping sees the full set
                parts = p_list_path.split(".")
                target = result
                for part in parts[:-1]:
                    if isinstance(target, dict):
                        target = target.get(part, {})
                if isinstance(target, dict) and parts:
                    target[parts[-1]] = all_items

            # Store result in cache after successful fetch + pagination.
            if cache_key and not cache_hit:
                _NODE_RESPONSE_CACHE[cache_key] = (time.time() + cache_ttl, result)
                log.info("[api_call] node=%s cached result (key=%.8s ttl=%ds)", node_id, cache_key, cache_ttl)

            # Apply response_mapping → populate state.collected
            updates: dict[str, Any] = {}
            for mapping in response_mapping:
                src_path = mapping["from"].lstrip("$").lstrip(".")
                dst_path = mapping["to"].removeprefix("collected.")
                # Use list-aware getter when path contains a wildcard
                if "[*]" in src_path or (".*" in src_path):
                    value = _jsonpath_get_list(result, src_path) if src_path else result
                else:
                    value = _jsonpath_get(result, src_path) if src_path else result
                # find_where: filter a list to the first item where item[field] == ctx value.
                # Useful when the API returns all records and the desired record must be
                # located client-side (e.g. Karmayogi enrollment list ignores courseId filter).
                # Example: find_where: {field: courseId, equals_ctx: collected.c1_course_id}
                find_where = mapping.get("find_where")
                if find_where and isinstance(value, list):
                    fw_field = find_where.get("field")
                    fw_ctx_path = find_where.get("equals_ctx", "")
                    # Resolve the target value from ctx (dotted path, e.g. collected.c1_course_id)
                    target_val = _resolve_ctx_path(ctx, fw_ctx_path)
                    value = next(
                        (item for item in value if isinstance(item, dict) and item.get(fw_field) == target_val),
                        None,
                    )
                # sub_path: after find_where (or any list-narrowing step), extract a
                # sub-field from the matched dict before applying transforms.
                sub_path = mapping.get("sub_path")
                if sub_path and isinstance(value, dict):
                    value = value.get(sub_path)
                # Apply named transform if specified.
                # NOTE: transforms run even when value is None — each transform
                # is responsible for handling None (e.g. unix_ms_to_iso returns
                # None, extract_incomplete_ids returns [], enrollment_status_to_int
                # returns 0). This ensures None status values still get normalised
                # to 0 rather than being stored as None and silently skipping branches.
                #
                # transform_ctx_key: optional dotted path (e.g. collected.course_id)
                # resolved from ctx and passed as the second argument to the transform.
                # Enables "context-aware" transforms such as filtering a list by a
                # collected field (e.g. kp_status_by_id needs both kpList and course_id).
                transform = mapping.get("transform")
                transform_ctx_key = mapping.get("transform_ctx_key")
                if transform:
                    transform_fn = _TRANSFORMS.get(transform)
                    if transform_fn is not None:
                        if transform_ctx_key:
                            ctx_val = _resolve_ctx_path(ctx, transform_ctx_key)
                            value = transform_fn(value, ctx_val)
                        else:
                            value = transform_fn(value)
                updates[dst_path] = value

            # Clear any stale error from a previous api_call so that this
            # node's on_success edge is taken correctly (not the on_error path
            # left over from an earlier timeout/error in the same conversation).
            updates["_last_api_error"] = ""

            return {
                "current_node": cfg["id"],
                "collected": {**state.collected, **updates},
            }

        return run

    def next_node(self, cfg: dict[str, Any]) -> str | None:
        # Happy-path next-edge. Error-path edges (timeout / not_found / any) are
        # routed via conditional edges if the YAML declares on_error.
        return cfg.get("on_success")

    def register_conditional_edges(self, graph: Any, cfg: dict[str, Any]) -> bool:
        """If `on_error` is declared, register conditional routing so failures
        go to the right node."""
        on_error = cfg.get("on_error")
        if not isinstance(on_error, dict):
            return False  # caller falls back to simple on_success edge

        on_success = cfg["on_success"]
        node_id = cfg["id"]

        def route(state: ConversationState) -> str:
            err = (state.collected or {}).get("_last_api_error", "")
            if not err:
                return on_success
            # Match against error keys: timeout / not_found / any
            for key, target in on_error.items():
                if err.startswith(key + ":") or err == key:
                    return target
            return on_error.get("any", on_success)

        targets = {on_success, *on_error.values()}
        graph.add_conditional_edges(node_id, route, {t: t for t in targets})
        return True


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

class IntegrationNotFound(Exception):
    """Raised by an adapter when the resource doesn't exist (404-like)."""


def _resolve_ctx_path(ctx: dict[str, Any], dotted_path: str) -> Any:
    """Resolve a dotted context path (e.g. 'collected.c1_course_id') from ctx."""
    parts = [p for p in dotted_path.split(".") if p]
    cur: Any = ctx
    for part in parts:
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
        if cur is None:
            return None
    return cur


def _state_ctx(state: ConversationState) -> dict[str, Any]:
    return {
        "collected": state.collected,
        "counters": state.counters,
        "user_id_hash": state.user_id_hash,
        "channel": state.channel,
        "session_id": str(state.session_id),
        "flow_id": state.flow_id,
        "language": state.language,
        # ticket_draft is populated by transfer_llm; exposed so _zoho_ticket.yaml
        # can use {{ ctx.ticket_draft.subject }} / {{ ctx.ticket_draft.description }}
        "ticket_draft": state.ticket_draft.model_dump() if state.ticket_draft else {},
    }


def _render_request(
    request_cfg: dict[str, Any],
    ctx: dict[str, Any],
    env_vars: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Render Jinja templates inside the request block (url, params, body, headers).

    ``env_vars`` is exposed as the top-level ``env`` variable so YAML templates
    can use ``{{ env.ZOHO_DEPARTMENT_ID }}`` etc.
    """
    extra = {"env": env_vars or {}}
    out: dict[str, Any] = {
        "method": request_cfg["method"].upper(),
        "url": render(request_cfg["url"], ctx, extra_vars=extra),
    }
    if "params" in request_cfg and request_cfg["params"]:
        out["params"] = {k: render(str(v), ctx, extra_vars=extra) for k, v in request_cfg["params"].items()}
    if "body" in request_cfg and request_cfg["body"]:
        out["body"] = _render_value(request_cfg["body"], ctx, extra_vars=extra)
    if "headers" in request_cfg and request_cfg["headers"]:
        out["headers"] = {k: render(str(v), ctx, extra_vars=extra) for k, v in request_cfg["headers"].items()}
    return out


def _render_value(value: Any, ctx: dict[str, Any], extra_vars: dict[str, Any] | None = None) -> Any:
    """Recursively render Jinja templates in nested dicts/lists/strings.

    Uses `render_native` for string values so that collected fields containing
    lists or dicts (e.g. `incomplete_ids`) are passed as proper JSON types in
    request bodies rather than being coerced to their string representation.

    Dict keys containing Jinja expressions (``{{ ... }}``) are also rendered,
    enabling dynamic filter keys such as:
        ``"{{ 'email' if ctx.collected.update_type == 'EMAIL' else 'phone' }}": "value"``
    """
    if isinstance(value, str):
        from app.engine.template import render_native
        return render_native(value, ctx, extra_vars=extra_vars)
    if isinstance(value, dict):
        return {
            render(k, ctx, extra_vars=extra_vars) if isinstance(k, str) and '{{' in k else k:
            _render_value(v, ctx, extra_vars=extra_vars)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_render_value(v, ctx, extra_vars=extra_vars) for v in value]
    return value


def _record_error(state: ConversationState, cfg: dict[str, Any], error: str) -> dict[str, Any]:
    log.warning("[api_call] node=%s recording error → %r (will route via on_error)", cfg["id"], error)
    return {
        "current_node": cfg["id"],
        "collected": {**state.collected, "_last_api_error": error},
    }


def _extract_incomplete_ids(lang_content_status: Any) -> list[str]:
    """Extract resource IDs where completion status != 2 from langContentStatus.

    langContentStatus shape: {"en": {"resource_id": 0|1|2, ...}, ...}
    Values: 0 = not started, 1 = in progress, 2 = completed.
    Returns deduplicated list of IDs that are not yet completed.
    """
    if not isinstance(lang_content_status, dict):
        return []
    ids: set[str] = set()
    for resources in lang_content_status.values():
        if isinstance(resources, dict):
            for resource_id, status in resources.items():
                if status != 2:
                    ids.add(resource_id)
    return list(ids)


def _extract_completed_ids(lang_content_status: Any) -> list[str]:
    """Extract resource IDs where completion status == 2 from langContentStatus.

    langContentStatus shape: {"en": {"resource_id": 0|1|2, ...}, ...}
    Values: 0 = not started, 1 = in progress, 2 = completed.
    Returns deduplicated list of IDs that are fully completed.
    """
    if not isinstance(lang_content_status, dict):
        return []
    ids: set[str] = set()
    for resources in lang_content_status.values():
        if isinstance(resources, dict):
            for resource_id, status in resources.items():
                if status == 2:
                    ids.add(resource_id)
    return list(ids)


def _extract_batch_id(batches: Any) -> str | None:
    """Extract batchId from the first element of a Karmayogi batches[] array.

    In-progress courses served by the enrollment list API return batchId
    nested inside a `batches` array rather than at the top-level of the
    course object.  This transform handles that case so the picker can
    populate `collected.batch_id` for all course states.

    batches shape: [{"batchId": "...", "name": "...", ...}, ...]
    Returns the batchId string from batches[0], or None if unavailable.
    """
    if not isinstance(batches, list) or len(batches) == 0:
        return None
    first = batches[0]
    if not isinstance(first, dict):
        return None
    return first.get("batchId")


def _diff_leaf_nodes(leaf_nodes: Any, completed_ids: Any) -> list[str]:
    """Return leaf node IDs that are not yet completed.

    leaf_nodes   — list of all resource IDs in the course (from content/read API).
    completed_ids — list of IDs the user has fully completed (status == 2 in
                    langContentStatus, stored via extract_completed_ids transform).

    Any leaf node absent from completed_ids is considered incomplete (not started
    or in progress). This cross-check catches resources the user has never opened
    and therefore do not appear in langContentStatus at all.
    """
    if not isinstance(leaf_nodes, list):
        return []
    done: set[str] = set(completed_ids) if isinstance(completed_ids, list) else set()
    return [rid for rid in leaf_nodes if rid not in done]


def _extract_all_names(names: Any) -> str:
    """Convert a list of resource names into a newline-separated bullet string.

    Used to surface all incomplete resource names in a single template variable
    instead of only content[0].name.

    Example output:
        - Introduction to Python
        - Module 2: Data Types
        - Final Assessment
    """
    if not isinstance(names, list):
        return str(names) if names else ""
    clean = [str(n) for n in names if n]
    return "\n".join(f"- {n}" for n in clean)


_SCORM_MIME = "application/vnd.ekstep.html-archive"


def _extract_scorm_resource_name(content_list: Any) -> str:
    """Return the name of the first SCORM resource in the content list.

    content_list is the full $.content[*] array (list of dicts).
    Returns empty string if no SCORM resource is found.
    """
    if not isinstance(content_list, list):
        return ""
    for item in content_list:
        if isinstance(item, dict) and item.get("mimeType") == _SCORM_MIME:
            return item.get("name") or ""
    return ""


def _extract_scorm_duration_minutes(content_list: Any) -> float:
    """Return the duration (in minutes) of the first SCORM resource in the content list.

    content_list is the full $.content[*] array (list of dicts).
    Returns 0.0 if no SCORM resource is found.
    """
    if not isinstance(content_list, list):
        return 0.0
    for item in content_list:
        if isinstance(item, dict) and item.get("mimeType") == _SCORM_MIME:
            return _duration_to_minutes(item.get("duration"))
    return 0.0


def _detect_assessment_only(content_list: Any) -> bool:
    """Return True if every item in the content list has an assessment primaryCategory.

    Used to route to the assessment guidance path only when ALL pending resources
    are assessments (i.e. all learning resources are already completed).
    content_list is the full $.content[*] array (list of dicts) from the
    composite search API (/api/composite/v4/search) response.

    Actual primaryCategory values from /api/composite/v4/search:
      'Course Assessment' — assessment / quiz resource           → Assessment Flow (SOP 8.1.2)
      'Course'            — regular course content resource      → check mimeType  (SOP 8.1.1)
                           (video/mp4, PDF, SCORM, etc.)

    Returns True only when EVERY item in the list is an assessment.
    Case-insensitive to handle minor API variance across environments.
    """
    if not isinstance(content_list, list) or not content_list:
        return False
    _ASSESSMENT_CATEGORIES = {"course assessment", "assessment"}
    return all(
        isinstance(item, dict)
        and str(item.get("primaryCategory") or "").lower().strip() in _ASSESSMENT_CATEGORIES
        for item in content_list
    )


def _calculate_remaining_attempts(result: Any) -> int:
    """Calculate remaining assessment attempts: allowed - made.

    If the result is missing or parsing fails, defaults to 0 (limit exceeded).
    """
    if not isinstance(result, dict):
        return 0
    allowed = result.get("attemptsAllowed", 0)
    made = result.get("attemptsMade", 0)
    try:
        remaining = int(allowed) - int(made)
        return remaining if remaining > 0 else 0
    except (ValueError, TypeError):
        return 0


def _duration_to_minutes(duration: Any) -> float:
    """Convert raw duration seconds (str or number) to rounded minutes."""
    try:
        return round(float(duration) / 60, 1)
    except (TypeError, ValueError):
        return 0.0


def _detect_scorm(mimetypes: Any) -> bool:
    """Return True if any mimeType in the list is the SCORM HTML-archive type."""
    scorm_mime = "application/vnd.ekstep.html-archive"
    if isinstance(mimetypes, list):
        return any(m == scorm_mime for m in mimetypes if m)
    return mimetypes == scorm_mime


def _unix_ms_to_iso(timestamp: Any) -> str | None:
    """Convert a Unix milliseconds timestamp (int or float) to an ISO 8601 UTC string.

    Karmayogi APIs return completedOn / enrolledDate as Unix ms integers.
    The expression evaluator's hours_since() helper expects ISO strings.
    Returns None if the value is None or cannot be converted.
    """
    if timestamp is None:
        return None
    try:
        from datetime import datetime, timezone
        dt = datetime.fromtimestamp(int(timestamp) / 1000.0, tz=timezone.utc)
        return dt.isoformat()
    except (TypeError, ValueError, OSError):
        return None


_ENROLLMENT_STATUS_MAP: dict[str, int] = {
    # String values returned by Karmayogi enrollment list API
    "notEnrolled":  0,
    "not enrolled": 0,
    "not started":  0,
    "NotEnrolled":  0,
    "In-Progress":  1,
    "in-progress":  1,
    "inprogress":   1,
    "Completed":    2,
    "completed":    2,
}


# ---------------------------------------------------------------------------
# Enrollment count transforms — used by multiple_account flow to summarise
# the courses list into simple integer counts for display.
# ---------------------------------------------------------------------------

def _count_courses_total(courses: Any) -> int:
    """Return the total number of In-Progress + Completed courses.

    Only counts courses with status 1 (In-Progress) or 2 (Completed) so that
    total always equals in_progress + completed, regardless of what the API returns.
    """
    if not isinstance(courses, list):
        return 0
    count = 0
    for course in courses:
        if isinstance(course, dict):
            status = _enrollment_status_to_int(course.get("status"))
            if status in (1, 2):
                count += 1
    return count


def _count_courses_inprogress(courses: Any) -> int:
    """Count courses with status == 1 (In-Progress) from the enrollment list."""
    if not isinstance(courses, list):
        return 0
    count = 0
    for course in courses:
        if isinstance(course, dict):
            status = _enrollment_status_to_int(course.get("status"))
            if status == 1:
                count += 1
    return count


def _count_courses_completed(courses: Any) -> int:
    """Count courses with status == 2 (Completed) from the enrollment list."""
    if not isinstance(courses, list):
        return 0
    count = 0
    for course in courses:
        if isinstance(course, dict):
            status = _enrollment_status_to_int(course.get("status"))
            if status == 2:
                count += 1
    return count


def _enrollment_status_to_int(status: Any) -> int:
    """Normalise Karmayogi enrollment status to 0/1/2 integer.

    The enrollment list API may return the status field as a string
    ("In-Progress", "Completed") or as a numeric code (0, 1, 2).
    This transform maps both representations to a consistent integer so
    that branch rules can use simple == comparisons.
      0 = not started / not enrolled
      1 = in progress
      2 = completed
    """
    if status is None:
        log.warning("[enrollment_status_to_int] status is None — defaulting to 0")
        return 0
    if isinstance(status, int):
        return status
    if isinstance(status, float):
        return int(status)
    if isinstance(status, str):
        if status in _ENROLLMENT_STATUS_MAP:
            return _ENROLLMENT_STATUS_MAP[status]
        normalised = status.lower().replace("-", "").replace(" ", "")
        for key, val in _ENROLLMENT_STATUS_MAP.items():
            if key.lower().replace("-", "").replace(" ", "") == normalised:
                return val
        log.warning("[enrollment_status_to_int] unrecognised status %r — defaulting to 0", status)
    else:
        log.warning("[enrollment_status_to_int] unexpected type %s for value %r — defaulting to 0", type(status).__name__, status)
    return 0  # Default: treat unknown as not started


def _extract_child_course_ids(children: Any) -> list[str]:
    """Extract identifiers of direct child courses/modules from a content hierarchy.

    Used with GET /api/content/v2/read/{course_id} response_mapping on
    ``$.content.children`` to get the list of child course/resource IDs for
    program progress checks.

    Returns a deduplicated list of identifier strings (empty list on failure).
    """
    if not isinstance(children, list):
        return []
    return [
        item["identifier"]
        for item in children
        if isinstance(item, dict) and item.get("identifier")
    ]


def _week_label_from_start(start_date_str: Any, week_index: int) -> str:
    """Compute a human-readable date range string for a specific week of the Insights API period.

    The Insights API returns a 4-week window with `startDate` (oldest Monday) and
    `endDate` (newest Sunday).  Weeks are indexed oldest-first:
      week_index 0 → w4 (oldest week, starts at startDate)
      week_index 1 → w3
      week_index 2 → w2
      week_index 3 → w1 (most recent week)

    Returns a string like "11 May – 17 May 2026" or "28 May – 03 Jun 2026".
    Falls back to a plain label when the date cannot be parsed.
    """
    from datetime import date, timedelta

    if not start_date_str or not isinstance(start_date_str, str):
        return f"Week {4 - week_index}"
    try:
        start = date.fromisoformat(start_date_str)
        week_start = start + timedelta(days=week_index * 7)
        week_end   = week_start + timedelta(days=6)
        # Format without leading zero on day (%-d is Linux/macOS specific)
        fmt_s = week_start.strftime("%-d %b")
        fmt_e = week_end.strftime("%-d %b %Y")
        return f"{fmt_s} – {fmt_e}"
    except (ValueError, TypeError):
        return f"Week {4 - week_index}"


def _week_label_w1(v: Any) -> str:
    """Most recent week (index 3 from startDate)."""
    return _week_label_from_start(v, 3)


def _week_label_w2(v: Any) -> str:
    """Second most recent week (index 2 from startDate)."""
    return _week_label_from_start(v, 2)


def _week_label_w3(v: Any) -> str:
    """Third week (index 1 from startDate)."""
    return _week_label_from_start(v, 1)


def _week_label_w4(v: Any) -> str:
    """Oldest week (index 0 from startDate)."""
    return _week_label_from_start(v, 0)


# ---------------------------------------------------------------------------
# Karma Points transforms
# ---------------------------------------------------------------------------

def _parse_addinfo(addinfo: Any) -> dict:
    """Parse the `addinfo` JSON string returned by the karma points API.

    Returns an empty dict on any parse failure.
    """
    import json as _json
    if not addinfo:
        return {}
    if isinstance(addinfo, dict):
        return addinfo
    if isinstance(addinfo, str):
        try:
            return _json.loads(addinfo)
        except Exception:
            return {}
    return {}


def _kp_status_by_id(kp_list: Any, course_id: Any) -> dict | None:
    """Scan a karma points list (``kpList``) for completion and rating entries
    matching *course_id* (``context_id`` field).

    Returns a dict:
        completion_credited  bool
        rating_credited      bool
        acbp                 bool | None   (None when completion not in list)
        has_assessment       bool | None
        completion_points    int  | None
        rating_points        int  | None
        course_name          str  | None   (from addinfo COURSENAME)

    Returns None when kp_list is not a valid list or course_id is missing.
    """
    if not isinstance(kp_list, list) or not course_id:
        return None

    cid = str(course_id).strip()
    completion: dict | None = None
    rating: dict | None = None

    for entry in kp_list:
        if not isinstance(entry, dict):
            continue
        if entry.get("context_id") != cid:
            continue
        op = entry.get("operation_type", "")
        if op == "COURSE_COMPLETION" and completion is None:
            completion = entry
        elif op == "RATING" and rating is None:
            rating = entry

    addinfo = _parse_addinfo(completion.get("addinfo") if completion else None)

    return {
        "completion_credited": completion is not None,
        "rating_credited":     rating is not None,
        "acbp":                addinfo.get("ACBP")       if completion else None,
        "has_assessment":      addinfo.get("ASSESSMENT") if completion else None,
        "completion_points":   completion.get("points")  if completion else None,
        "rating_points":       rating.get("points")      if rating     else None,
        "course_name":         addinfo.get("COURSENAME") if completion else (
            _parse_addinfo(rating.get("addinfo") if rating else None).get("COURSENAME")
        ),
    }


def _kp_monthly_rank(kp_list: Any, course_id: Any) -> int:
    """Return the 1-based rank of *course_id* among all COURSE_COMPLETION entries
    in the same calendar month (UTC).

    Rank 1 = first completion that month; rank 5+ means only the first four
    completions were eligible for karma points.

    Returns 0 when the course completion entry is not found.
    """
    from datetime import datetime, timezone

    if not isinstance(kp_list, list) or not course_id:
        return 0

    cid = str(course_id).strip()
    target_entry: dict | None = None

    for entry in kp_list:
        if isinstance(entry, dict) and entry.get("context_id") == cid and entry.get("operation_type") == "COURSE_COMPLETION":
            target_entry = entry
            break

    if target_entry is None:
        return 0

    target_ts = target_entry.get("credit_date")
    if not target_ts:
        return 1

    try:
        target_dt = datetime.fromtimestamp(int(target_ts) / 1000.0, tz=timezone.utc)
    except Exception:
        return 1

    target_month = (target_dt.year, target_dt.month)
    rank = 1

    for entry in kp_list:
        if not isinstance(entry, dict):
            continue
        if entry.get("operation_type") != "COURSE_COMPLETION":
            continue
        if entry.get("context_id") == cid:
            continue  # skip the target itself
        ts = entry.get("credit_date")
        if not ts:
            continue
        try:
            dt = datetime.fromtimestamp(int(ts) / 1000.0, tz=timezone.utc)
        except Exception:
            continue
        if (dt.year, dt.month) == target_month and ts <= target_ts:
            rank += 1

    return rank


def _kp_event_credited(kp_list: Any, event_id: Any) -> bool:
    """Return True if the user has a karma credit for the given event_id.

    Matches by context_id regardless of context_type (Event / Blended Program
    etc.) and any operation_type that represents event participation credit.
    """
    if not isinstance(kp_list, list) or not event_id:
        return False

    eid = str(event_id).strip()
    for entry in kp_list:
        if isinstance(entry, dict) and entry.get("context_id") == eid:
            return True
    return False


def _build_user_eligibility_ctx(response: Any) -> dict:
    """Build a flat eligibility context dict from the user profile response object.

    Input: the full $.response object from GET /api/user/private/v1/read/{user_id}
           (after the karmayogi adapter unwraps the top-level 'result' envelope).

    Output dict keys match the criteriaKey names used in accessControl.userGroups,
    plus additional keys used by the secureSettings metadata eligibility check:

      criteriaKey          source field
      -----------          ------------
      group              → profileDetails.professionalDetails[].group          (list)
      designation        → profileDetails.professionalDetails[].designation    (list)
      rootOrgId          → rootOrgId                                           (scalar)
      user / userid      → identifier (also userId)                            (scalar)
      department         → profileDetails.employmentDetails.departmentName     (scalar)
      cadre              → profileDetails.cadreDetails.cadreName               (scalar)
      service            → profileDetails.cadreDetails.civilServiceName        (scalar)
      batch              → profileDetails.cadreDetails.cadreBatch              (scalar, str)

    Additional keys used by check_secure_settings_eligibility (moderated courses):
      profile_status     → profileDetails.profileStatus                        (scalar, e.g. "VERIFIED")
      ministry_or_state_id → profileDetails.ministryOrStateId                 (scalar, org/ministry ID)
    """
    if not isinstance(response, dict):
        return {}

    profile_details: dict = response.get("profileDetails") or {}
    professional_list: list = profile_details.get("professionalDetails") or []
    employment: dict = profile_details.get("employmentDetails") or {}
    cadre: dict = profile_details.get("cadreDetails") or {}

    # Collect multi-valued fields from professionalDetails entries
    groups = [
        p["group"] for p in professional_list
        if isinstance(p, dict) and p.get("group")
    ]
    designations = [
        p["designation"] for p in professional_list
        if isinstance(p, dict) and p.get("designation")
    ]

    batch_raw = cadre.get("cadreBatch")
    batch_str = str(batch_raw) if batch_raw is not None else None

    user_id = (
        response.get("identifier")
        or response.get("userId")
        or response.get("id")
    )

    return {
        "group":               groups,
        "designation":         designations,
        "rootOrgId":           response.get("rootOrgId"),
        "user":                user_id,
        # 'userid' is an alias — stored separately so direct key lookup works
        "userid":              user_id,
        "department":          employment.get("departmentName"),
        "cadre":               cadre.get("cadreName"),
        "service":             cadre.get("civilServiceName"),
        "batch":               batch_str,
        # Additional fields for moderated course secureSettings check
        # profile_status: compared against secureSettings.isVerifiedKarmayogi
        "profile_status":      profile_details.get("profileStatus"),
        # ministry_or_state_id: the org/ministry ID — same value as rootOrgId on the
        # Karmayogi platform; stored separately to match against secureSettings.organisation
        "ministry_or_state_id": profile_details.get("ministryOrStateId") or response.get("rootOrgId"),
    }


def _check_user_eligibility(user_groups: Any, user_eligibility_ctx: Any) -> bool:
    """Return True if the user is eligible for the course / event based on access control.

    Called with:
      user_groups         – result.accessControl.userGroups  (list | None)
      user_eligibility_ctx – collected.user_eligibility_ctx  (dict built by
                             _build_user_eligibility_ctx from the User Read API response)

    Logic
    -----
    - userGroups is None / empty list  →  no restrictions  →  eligible (True)
    - OR  across userGroups : user must fully satisfy ALL criteria of at least one group
    - AND within a group    : every criteriaKey entry must have a matching value

    Supported criteriaKey values:
      group       → list  (any entry matches)
      designation → list  (any entry matches)
      rootOrgId   → scalar
      user/userid → scalar (user's identifier UUID)
      department  → scalar
      cadre       → scalar
      service     → scalar
      batch       → scalar (year as string, e.g. "1985")
    """
    if not user_groups or not isinstance(user_groups, list):
        # No access control configured → publicly accessible
        return True

    ctx: dict = user_eligibility_ctx if isinstance(user_eligibility_ctx, dict) else {}

    for group in user_groups:
        if not isinstance(group, dict):
            continue
        criteria_list: list = group.get("userGroupCriteriaList") or []

        if not criteria_list:
            # Group with no criteria → everyone satisfies it → eligible
            return True

        # AND: every criterion in this group must be satisfied
        group_matched = True
        for criterion in criteria_list:
            key: str = (criterion.get("criteriaKey") or "").strip()
            required_values: list = criterion.get("criteriaValue") or []
            if not required_values:
                continue  # no required values → criterion trivially satisfied

            user_value = ctx.get(key)

            if isinstance(user_value, list):
                # Multi-valued field (group, designation) — any match is enough
                criterion_matched = any(v in required_values for v in user_value)
            else:
                # Scalar field (rootOrgId, user, department, cadre, service, batch)
                criterion_matched = user_value in required_values

            if not criterion_matched:
                group_matched = False
                break

        if group_matched:
            return True  # OR: matched this group → eligible

    return False  # No group matched → not eligible


def _check_secure_settings_eligibility(secure_settings: Any, user_eligibility_ctx: Any) -> bool:
    """Return True if the user satisfies the moderated course secureSettings metadata criteria.

    Called with:
      secure_settings      – content[0].secureSettings from composite search response
      user_eligibility_ctx – collected.user_eligibility_ctx (dict built by
                             _build_user_eligibility_ctx from the User Read API response)

    secureSettings structure (from composite search):
      {
        "isVerifiedKarmayogi": "Yes" | "No",  # if "Yes" → user profileStatus must be "VERIFIED"
        "organisation": ["<rootOrgId>", ...],  # list of eligible org IDs
        "version": 1
      }

    Logic
    -----
    - secure_settings is None / empty / not a dict  → not a moderated course  → True
    - organisation list present and non-empty:
        user's rootOrgId OR ministryOrStateId must appear in the list
    - isVerifiedKarmayogi == "Yes":
        user's profileDetails.profileStatus must equal "VERIFIED" (case-insensitive)
    - ALL applicable checks must pass (AND logic across criteria)
    """
    if not secure_settings or not isinstance(secure_settings, dict):
        return True  # No secureSettings → not a moderated course → eligible

    ctx: dict = user_eligibility_ctx if isinstance(user_eligibility_ctx, dict) else {}
    all_passed = True

    # --- Organisation / Ministry check -------------------------------------------
    # secureSettings.organisation is a list of rootOrgId strings that are eligible.
    # The user matches if their rootOrgId OR ministryOrStateId appears in the list.
    eligible_orgs: list = secure_settings.get("organisation") or []
    if eligible_orgs:
        user_root_org   = ctx.get("rootOrgId")
        user_ministry   = ctx.get("ministry_or_state_id")
        org_matched = (
            (user_root_org  is not None and user_root_org  in eligible_orgs)
            or
            (user_ministry  is not None and user_ministry  in eligible_orgs)
        )
        if not org_matched:
            log.debug(
                "[check_secure_settings_eligibility] org check failed: "
                "user rootOrgId=%r ministryOrStateId=%r not in %r",
                user_root_org, user_ministry, eligible_orgs,
            )
            all_passed = False

    # --- Verified Karmayogi check ------------------------------------------------
    # If isVerifiedKarmayogi == "Yes", only users with profileStatus == "VERIFIED" can access.
    is_verified_required = str(secure_settings.get("isVerifiedKarmayogi") or "").strip().lower()
    if is_verified_required == "yes":
        user_profile_status = str(ctx.get("profile_status") or "").strip().upper()
        if user_profile_status != "VERIFIED":
            log.debug(
                "[check_secure_settings_eligibility] verified karmayogi check failed: "
                "user profileStatus=%r (required VERIFIED)",
                ctx.get("profile_status"),
            )
            all_passed = False

    return all_passed


def _has_issued_certificates(issued_certificates: Any) -> bool:
    """Return True if the enrollment's issuedCertificates list contains at least one entry.

    The Karmayogi enrollment list API returns ``issuedCertificates`` as a list of
    certificate objects (each with identifier, lastIssuedOn, name, token, version).
    An empty list or null means no certificate has been generated yet.

    Used as a response_mapping transform on
    ``$.result.enrollments[0].issuedCertificates``  →  collected.certificate_issued

    Returns:
        True  – certificate has been generated (list is non-empty)
        False – not yet generated (None / empty list / unexpected type)
    """
    if not issued_certificates:
        return False
    if isinstance(issued_certificates, list):
        return len(issued_certificates) > 0
    # Defensive: treat any truthy scalar as "issued" (handles legacy bool/string)
    return bool(issued_certificates)


def _extract_consumption_records(records: Any) -> list[dict]:
    """Normalise consumptionRecords from Admin Content State API into a clean list.

    Admin Content State API response shape:
        result.consumptionRecords[
            {
                "contentid": "do_xxx",
                "language":  "english",
                "status":    2,          # 0 = not started, 1 = in-progress, 2 = completed
                "completionpercentage": 100.0,
                ...other fields...
            },
            ...
        ]

    Returns a list of dicts with exactly three keys: contentid, language, status.
    Unknown / malformed items are silently skipped.
    """
    if not isinstance(records, list):
        return []
    out = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        contentid = rec.get("contentid") or rec.get("contentId")
        language  = rec.get("language", "")
        status    = rec.get("status")
        if contentid is None or status is None:
            continue
        out.append({
            "contentid": str(contentid),
            "language":  str(language).lower(),
            "status":    int(status),
        })
    return out


def _append_consumption_records(new_records: Any, existing_records: Any) -> list[dict]:
    """Append new consumption records to an existing list without overwriting earlier results.

    Used in the Program child-course loop (``api_admin_content_state_loop``) where
    Admin Content State API is called once per child Course DO_ID. Each call produces
    a separate set of consumption records; this transform merges them all into a single
    ``collected.admin_content_states`` list before running the technical-issue comparison.

    new_records      — raw ``consumptionRecords[*]`` from the current API response.
    existing_records — current value of ``collected.admin_content_states`` passed via
                       ``transform_ctx_key: collected.admin_content_states`` in YAML.
                       None / empty list on the first loop iteration.
    """
    new = _extract_consumption_records(new_records)
    existing = existing_records if isinstance(existing_records, list) else []
    return existing + new


def _compare_enrollment_vs_admin_state(
    lang_content_status: Any,
    admin_content_states: Any,
) -> bool:
    """Detect a technical issue by cross-referencing Enrollment and Admin Content State data.

    INPUTS
    ------
    lang_content_status  — raw ``langContentStatus`` object captured from the Enrollment
                           API's dynamic picker (stored as ``collected.lang_content_status``).
                           Shape: { "<language>": { "<do_id>": <status_int>, ... }, ... }
                           where status 1 = In-Progress, 2 = Completed.

    admin_content_states — list produced by the ``extract_consumption_records`` transform
                           applied to the Admin Content State API response.
                           Each element: { "contentid": str, "language": str, "status": int }
                           where status 2 = completed on the backend.

    LOGIC
    -----
    For every (language, do_id) pair in langContentStatus:
      - If enrollment status == 1 (In-Progress) AND
        the Admin Content State API shows status == 2 (completed on server)
        → TECHNICAL ISSUE detected → return True immediately.

    A mismatch of this kind means:
      "The backend/server has recorded the resource as completed, but the
       portal still shows it as In-Progress for the learner."

    Returns False when:
      - Either input is missing / malformed.
      - No mismatch is found after checking all resources.
    """
    if not isinstance(lang_content_status, dict) or not isinstance(admin_content_states, list):
        log.debug(
            "[compare_enrollment_vs_admin_state] invalid inputs — "
            "lang_content_status type=%s, admin_content_states type=%s",
            type(lang_content_status).__name__,
            type(admin_content_states).__name__,
        )
        return False

    # Build a lookup: (contentid_lower, language_lower) → admin_status
    admin_lookup: dict[tuple[str, str], int] = {}
    for rec in admin_content_states:
        if isinstance(rec, dict):
            cid  = str(rec.get("contentid", "")).lower()
            lang = str(rec.get("language", "")).lower()
            st   = rec.get("status")
            if cid and st is not None:
                admin_lookup[(cid, lang)] = int(st)

    # Iterate over langContentStatus and compare per-resource
    for lang, resources in lang_content_status.items():
        if not isinstance(resources, dict):
            continue
        lang_lower = str(lang).lower()
        for do_id, enroll_status in resources.items():
            # Only check resources that are still In-Progress from the learner's view
            if int(enroll_status) != 1:
                continue
            do_id_lower = str(do_id).lower()
            # Look up admin status — try language-specific first, then without language
            admin_status = admin_lookup.get((do_id_lower, lang_lower))
            if admin_status is None:
                # Fallback: match on contentid alone (language field may differ)
                admin_status = next(
                    (v for (cid, _), v in admin_lookup.items() if cid == do_id_lower),
                    None,
                )
            if admin_status == 2:
                log.info(
                    "[compare_enrollment_vs_admin_state] MISMATCH — "
                    "do_id=%s lang=%s enroll_status=1 admin_status=2 → technical issue",
                    do_id, lang,
                )
                return True

    log.debug(
        "[compare_enrollment_vs_admin_state] no technical issue found — "
        "checked %d enrollment resource(s) against %d admin record(s)",
        sum(len(v) for v in lang_content_status.values() if isinstance(v, dict)),
        len(admin_content_states),
    )
    return False


def _extract_incomplete_child_courses(cap_content: Any, all_enrollment_list: Any) -> list[dict]:
    """Return a list of dicts {"courseId": ..., "batchId": ...} for incomplete CAP child courses.
       If the CAP has no children, treats the CAP itself as the course to check.
    """
    if not isinstance(cap_content, dict) or not isinstance(all_enrollment_list, list):
        return []
    
    children = cap_content.get("children", [])
    if not children:
        children = [cap_content]
    
    incomplete = []
    for child in children:
        if not isinstance(child, dict): continue
        if "assessment" in str(child.get("name", "")).lower(): continue
        
        cid = child.get("identifier")
        if not cid: continue
        
        is_complete = False
        batch_id = None
        for enroll in all_enrollment_list:
            if not isinstance(enroll, dict): continue
            if enroll.get("courseId") == cid:
                batch_id = enroll.get("batchId")
                status = enroll.get("status")
                pct = enroll.get("completionPercentage")
                certs = enroll.get("issuedCertificates")
                if status == 2 or pct == 100 or (isinstance(certs, list) and len(certs) > 0):
                    is_complete = True
                break
                
        if not is_complete:
            incomplete.append({"courseId": cid, "batchId": batch_id})
            
    return incomplete


def _check_cap_technical_issue(all_enrollments: Any, admin_states: Any, course_id: Any) -> bool:
    if not isinstance(all_enrollments, list) or not course_id:
        return False
    for enroll in all_enrollments:
        if isinstance(enroll, dict) and enroll.get("courseId") == course_id:
            lang_status = enroll.get("langContentStatus", {})
            return _compare_enrollment_vs_admin_state(lang_status, admin_states)
    return False


def _filter_by_incomplete_ids(content_list: Any, incomplete_ids: Any) -> list:
    """Filter a composite-search content list to only items whose identifier
    appears in ``incomplete_ids``.

    Used when the composite search is executed against the course DO_ID (which
    returns ALL resources — complete and incomplete alike) so that SCORM/assessment
    detection only considers the resources the user has not yet finished.

    If ``incomplete_ids`` is empty or missing the full list is returned unchanged
    so callers always receive a usable list.
    """
    if not isinstance(content_list, list):
        return []
    id_set = set(incomplete_ids) if isinstance(incomplete_ids, list) and incomplete_ids else None
    if id_set is None:
        return content_list
    return [item for item in content_list if isinstance(item, dict) and item.get("identifier") in id_set]


def _detect_scorm_filtered(content_list: Any, incomplete_ids: Any) -> bool:
    """detect_scorm applied only to the incomplete subset of a course content list."""
    filtered = _filter_by_incomplete_ids(content_list, incomplete_ids)
    return _detect_scorm([item.get("mimeType") for item in filtered])


def _extract_all_names_filtered(content_list: Any, incomplete_ids: Any) -> str:
    """extract_all_names applied only to the incomplete subset of a course content list."""
    filtered = _filter_by_incomplete_ids(content_list, incomplete_ids)
    return _extract_all_names([item.get("name") for item in filtered])


def _extract_scorm_resource_name_filtered(content_list: Any, incomplete_ids: Any) -> str:
    """extract_scorm_resource_name applied only to the incomplete subset of a course content list."""
    filtered = _filter_by_incomplete_ids(content_list, incomplete_ids)
    return _extract_scorm_resource_name(filtered)


def _extract_scorm_duration_filtered(content_list: Any, incomplete_ids: Any) -> float:
    """extract_scorm_duration_minutes applied only to the incomplete subset of a course content list."""
    filtered = _filter_by_incomplete_ids(content_list, incomplete_ids)
    return _extract_scorm_duration_minutes(filtered)


def _detect_assessment_only_filtered(content_list: Any, incomplete_ids: Any) -> bool:
    """detect_assessment_only applied only to the incomplete subset of a course content list."""
    filtered = _filter_by_incomplete_ids(content_list, incomplete_ids)
    return _detect_assessment_only(filtered)


def _extract_hierarchy_names(hierarchy: Any, incomplete_ids: Any) -> str:
    """Fallback transformer: traverse course hierarchy and extract names matching incomplete_ids."""
    if isinstance(hierarchy, dict) and "content" in hierarchy:
        hierarchy = hierarchy.get("content")
    if not hierarchy or not incomplete_ids:
        return ""
    
    id_list = incomplete_ids if isinstance(incomplete_ids, list) else [incomplete_ids]
    names = []
    
    def traverse(node: Any) -> None:
        if not isinstance(node, dict):
            return
        if node.get("identifier") in id_list:
            name = node.get("name")
            if name and name not in names:
                names.append(name)
        children = node.get("children", [])
        if isinstance(children, list):
            for child in children:
                traverse(child)
                
    traverse(hierarchy)
    return ", ".join(names)


def _flatten_cadre_services(value: Any) -> list[dict]:
    """Flatten cadreConfig nested service list into [{id, name}, ...].

    Input: result.response.value (after karmayogi.py unwraps result envelope).
    Traversal: civilServiceType.civilServiceTypeList[].serviceList[]
    """
    if not isinstance(value, dict):
        return []
    type_list = value.get("civilServiceType", {}).get("civilServiceTypeList", [])
    services = []
    for service_type in type_list:
        for service in service_type.get("serviceList", []):
            sid  = service.get("id", "")
            name = service.get("name", "")
            if sid and name:
                services.append({"id": sid, "name": name})
    return services


def _extract_event_time_spent(event: Any) -> float | None:
    """Extract time spent (duration) from an event's progress details.

    Checks both top-level 'lrcProgressDetails' and 'userEventConsumption[0].progressdetails'.
    Parses the JSON string and extracts the 'duration' field.
    Returns None when the field is absent so branch rules can distinguish
    "no data available" from "truly zero time spent".
    """
    if not isinstance(event, dict):
        return None

    def parse_duration(details_str: Any) -> float | None:
        if isinstance(details_str, str):
            try:
                import json
                data = json.loads(details_str)
                val = data.get("duration")
                if val is not None:
                    return float(val)
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        return None

    dur1 = parse_duration(event.get("lrcProgressDetails"))
    if dur1 is not None:
        return dur1

    consumptions = event.get("userEventConsumption", [])
    if isinstance(consumptions, list) and len(consumptions) > 0:
        first = consumptions[0]
        if isinstance(first, dict):
            dur2 = parse_duration(first.get("progressdetails"))
            if dur2 is not None:
                return dur2

    return None


def _is_youtube_embed_url(url: Any) -> bool:
    """Return True if the URL is a valid YouTube embed link."""
    if not isinstance(url, str):
        return False
    import re
    return bool(re.search(r"youtube\.com/embed/", url, re.IGNORECASE))


def _filter_orgs_by_parent(orgs: Any, parent_id: Any) -> list[dict]:
    """Filter list of organizations by parent ministry or state ID."""
    if not isinstance(orgs, list):
        return []
    filtered = []
    for org in orgs:
        if not isinstance(org, dict):
            continue
        # Check multiple possible parent ID field names for flexibility
        if (org.get("ministryOrStateId") == parent_id or 
            org.get("parentOrgId") == parent_id or
            org.get("levelZeroOrgId") == parent_id or
            org.get("rootOrgId") == parent_id or
            org.get("parentId") == parent_id):
            filtered.append(org)
    # Always append the static "Others" option to the end of the filtered list
    filtered.append({
        "identifier": "other_org",
        "orgName": "Others"
    })
    return filtered


def _merge_lists(value: Any, existing_list: Any) -> list:
    """Merge two lists of dicts."""
    if not isinstance(existing_list, list):
        existing_list = []
    if isinstance(value, list):
        return existing_list + value
    return existing_list


def _append_others_org(orgs: Any) -> list[dict]:
    """Append the 'Others' option to the end of the organizations list."""
    if not isinstance(orgs, list):
        filtered = []
    else:
        filtered = list(orgs)
    
    filtered.append({
        "identifier": "other_org",
        "orgName": "Others"
    })
    return filtered


# Registry of named transforms usable in YAML response_mapping `transform:` field.
_TRANSFORMS: dict[str, Any] = {
    "extract_hierarchy_names":     _extract_hierarchy_names,
    "extract_incomplete_child_courses": _extract_incomplete_child_courses,
    "extract_incomplete_ids":      _extract_incomplete_ids,
    "extract_completed_ids":           _extract_completed_ids,
    # Extracts batchId from batches[0] for in-progress courses where batchId
    # is nested inside the batches[] array instead of at the course root level.
    "extract_batch_id":            _extract_batch_id,
    "diff_leaf_nodes":                 _diff_leaf_nodes,
    "extract_all_names":               _extract_all_names,
    "extract_scorm_resource_name":     _extract_scorm_resource_name,
    "extract_scorm_duration_minutes":  _extract_scorm_duration_minutes,
    "detect_assessment_only":          _detect_assessment_only,
    "calculate_remaining_attempts":    _calculate_remaining_attempts,
    "duration_to_minutes":            _duration_to_minutes,
    "detect_scorm":                _detect_scorm,
    "unix_ms_to_iso":              _unix_ms_to_iso,
    "enrollment_status_to_int":    _enrollment_status_to_int,
    "count_courses_total":         _count_courses_total,
    "count_courses_inprogress":    _count_courses_inprogress,
    "count_courses_completed":     _count_courses_completed,
    "extract_child_course_ids":    _extract_child_course_ids,
    # Certificate check — converts issuedCertificates list → bool
    # True  = non-empty list (certificate generated)
    # False = null / empty list (not yet generated)
    "has_issued_certificates":     _has_issued_certificates,
    # Admin Content State API — consumption record normalisation
    # Converts result.consumptionRecords[*] → list of {contentid, language, status}
    "extract_consumption_records":       _extract_consumption_records,
    # Admin Content State API — loop accumulator for multi-course Programs
    # Appends new records to existing collected.admin_content_states list.
    # Requires transform_ctx_key: collected.admin_content_states in YAML.
    "append_consumption_records":        _append_consumption_records,
    # Admin Content State API vs Enrollment API cross-comparison
    # Detects technical issues: enrollment In-Progress + admin Completed
    # Note: This transform is called directly in branch rule expressions via
    #       compare_enrollment_vs_admin_state() rather than as a response_mapping transform.
    "compare_enrollment_vs_admin_state": _compare_enrollment_vs_admin_state,
    # Weekly Clap — Insights API week date-range labels
    "week_label_w1": _week_label_w1,
    "week_label_w2": _week_label_w2,
    "week_label_w3": _week_label_w3,
    "week_label_w4": _week_label_w4,
    # Composite search filtered transforms — query by course DO_ID, filter to incomplete subset
    # All five require transform_ctx_key: collected.incomplete_ids in YAML.
    "detect_scorm_filtered":                  _detect_scorm_filtered,
    "extract_all_names_filtered":             _extract_all_names_filtered,
    "extract_scorm_resource_name_filtered":   _extract_scorm_resource_name_filtered,
    "extract_scorm_duration_filtered":        _extract_scorm_duration_filtered,
    "detect_assessment_only_filtered":        _detect_assessment_only_filtered,
    # Karma Points — context-aware transforms (require transform_ctx_key in YAML)
    "kp_status_by_id":   _kp_status_by_id,   # (kp_list, course_id) → dict
    "kp_monthly_rank":   _kp_monthly_rank,   # (kp_list, course_id) → int
    "kp_event_credited": _kp_event_credited, # (kp_list, event_id)  → bool
    # Access control — two-step: build ctx from user profile, then check eligibility
    # Step 1: applied in _karmayogi_user.yaml on $.response  → collected.user_eligibility_ctx
    "build_user_eligibility_ctx": _build_user_eligibility_ctx,  # (response) → dict
    # Step 2a: applied in access settings nodes (requires transform_ctx_key: collected.user_eligibility_ctx)
    "check_user_eligibility": _check_user_eligibility,  # (userGroups, user_eligibility_ctx) → bool
    # Step 2b: applied on composite search secureSettings for moderated courses
    # (requires transform_ctx_key: collected.user_eligibility_ctx)
    # Checks organisation list + isVerifiedKarmayogi flag against user profile
    "check_secure_settings_eligibility": _check_secure_settings_eligibility,  # (secureSettings, user_eligibility_ctx) → bool
    # cadreConfig master list flattening
    "flatten_cadre_services":        _flatten_cadre_services,
    # Event related issues SOP transforms
    "extract_event_time_spent":      _extract_event_time_spent,
    "is_youtube_embed_url":          _is_youtube_embed_url,
    "filter_orgs_by_parent":         _filter_orgs_by_parent,
    "append_others_org":             _append_others_org,
    "merge_lists":                   _merge_lists,
}



def _jsonpath_get(data: Any, path: str) -> Any:
    """JSONPath subset — supports dotted paths, [N] bracket indices, and [*] wildcard.

    Examples:
      courses.0.courseName        → data["courses"][0]["courseName"]
      courses[0].courseName       → same (bracket notation converted)
      content[*].mimeType         → [item["mimeType"] for item in data["content"]]
    """
    import re

    if not path:
        return data

    # Normalise bracket notation: "courses[0].name" → "courses.0.name"
    #                              "content[*].mimeType" → "content.*.mimeType"
    path = re.sub(r"\[(\d+)\]", r".\1", path)
    path = re.sub(r"\[\*\]", ".*", path)

    cur = data
    for part in path.split("."):
        if part == "":
            continue
        # Wildcard: collect the field from every item in a list
        if part == "*":
            if isinstance(cur, list):
                next_part_remaining = None  # consumed inline below
                return cur  # caller will handle list; return as-is for chaining
            return None
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            # Numeric index
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if cur is None:
            return None
    return cur


def _jsonpath_get_list(data: Any, path: str) -> list[Any]:
    """Like _jsonpath_get but handles a trailing wildcard to return a flat list.

    e.g. "content.*.mimeType" → [item["mimeType"] for item in data["content"]]
    """
    import re

    path = re.sub(r"\[(\d+)\]", r".\1", path)
    path = re.sub(r"\[\*\]", ".*", path)

    parts = [p for p in path.split(".") if p]
    cur: Any = data
    for i, part in enumerate(parts):
        if part == "*":
            if not isinstance(cur, list):
                return []
            remaining = ".".join(parts[i + 1:])
            if remaining:
                return [_jsonpath_get(item, remaining) for item in cur if item is not None]
            return cur
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return []
        else:
            return []
        if cur is None:
            return []
    return [cur] if cur is not None else []

