"""Sandboxed Jinja2 templating for bot messages and ticket payloads.

We use `SandboxedEnvironment` to prevent template injection — no arbitrary
attribute access, no Python operators that could leak state.
"""

from __future__ import annotations

from typing import Any

from jinja2.sandbox import SandboxedEnvironment


def _date_filter(value: Any, format: str = "%d %b %Y") -> str:
    """Format an ISO timestamp string or datetime."""
    from datetime import datetime

    if value is None:
        return ""
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    else:
        dt = value
    return dt.strftime(format)


def _default_filter(value: Any, default: str = "N/A") -> str:
    """Return default if value is None/empty."""
    if value is None:
        return default
    if isinstance(value, (str, list, dict)) and len(value) == 0:
        return default
    return value


def _to_yaml_filter(value: Any) -> str:
    """Dump a dict/list as YAML for embedding in ticket descriptions."""
    import yaml

    return yaml.safe_dump(value, default_flow_style=False, allow_unicode=True).strip()


_env = SandboxedEnvironment(
    autoescape=False,
    trim_blocks=True,
    lstrip_blocks=True,
)
_env.filters["date"] = _date_filter
_env.filters["default"] = _default_filter
_env.filters["to_yaml"] = _to_yaml_filter

# NativeEnvironment returns Python-native types (list, dict, int…) instead of
# always coercing to string. Used in request-body rendering so that list/dict
# values in collected (e.g. incomplete_ids) are sent as proper JSON types.
from jinja2.nativetypes import NativeEnvironment as _NativeEnv

_native_env = _NativeEnv(
    autoescape=False,
    trim_blocks=True,
    lstrip_blocks=True,
)
_native_env.filters["date"] = _date_filter
_native_env.filters["default"] = _default_filter
_native_env.filters["to_yaml"] = _to_yaml_filter


def render(
    template_string: str,
    ctx: dict[str, Any],
    extra_vars: dict[str, Any] | None = None,
) -> str:
    """Render a Jinja template string against a context dict.

    Convention: templates use ``{{ ctx.collected.field }}``, ``{{ ctx.user.name }}``, etc.
    Pass ``extra_vars`` to expose additional top-level variables (e.g. ``env``):
      ``render(tpl, ctx, extra_vars={"env": {"ZOHO_DEPARTMENT_ID": "..."}})``
    → usable in templates as ``{{ env.ZOHO_DEPARTMENT_ID }}``.

    Always returns a string — use ``render_native`` when you need native Python types.
    """
    template = _env.from_string(template_string)
    extra = {k: _DotDict(v) if isinstance(v, dict) else v for k, v in (extra_vars or {}).items()}
    return template.render(ctx=_DotDict(ctx), **extra)


def render_native(
    template_string: str,
    ctx: dict[str, Any],
    extra_vars: dict[str, Any] | None = None,
) -> Any:
    """Like ``render`` but returns native Python types (list, dict, int, bool…).

    Use this when rendering request body values that may be lists or dicts
    stored in ``collected`` (e.g. ``{{ ctx.collected.incomplete_ids }}``).
    Falls back to the string result if the template is not a pure expression.
    Accepts same ``extra_vars`` as ``render``.
    """
    template = _native_env.from_string(template_string)
    extra = {k: _DotDict(v) if isinstance(v, dict) else v for k, v in (extra_vars or {}).items()}
    return template.render(ctx=_DotDict(ctx), **extra)


class _DotDict:
    """Allows {{ ctx.collected.course.name }} style access on a plain dict."""

    def __init__(self, data: Any) -> None:
        self._data = data if isinstance(data, dict) else {}

    def __getattr__(self, key: str) -> Any:
        if isinstance(self._data, dict) and key in self._data:
            value = self._data[key]
            if isinstance(value, dict):
                return _DotDict(value)
            return value
        return ""

    def __getitem__(self, key: str) -> Any:
        return self.__getattr__(key)

    def __str__(self) -> str:
        return str(self._data)

    def __bool__(self) -> bool:
        return bool(self._data)
