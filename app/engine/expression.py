"""Sandboxed expression evaluation for branch rules.

Used by `branch` and `increment_and_branch` nodes. Supports a restricted Python
expression subset over `state.collected`, `state.counters`, plus a set of
registered helper functions.

We use `simpleeval` for sandboxing — no `eval`, no imports, no attribute access
beyond what we allow.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from simpleeval import EvalWithCompoundTypes


def _hours_since(timestamp: str | datetime) -> float:
    """Hours between a timestamp and now (UTC)."""
    if isinstance(timestamp, str):
        try:
            ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            return -1.0
    else:
        ts = timestamp
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - ts
    return delta.total_seconds() / 3600.0


def _days_since(timestamp: str | datetime) -> float:
    return _hours_since(timestamp) / 24.0


def _has(value: Any) -> bool:
    """Truthy + non-empty check."""
    if value is None:
        return False
    if isinstance(value, (str, list, dict)):
        return len(value) > 0
    return bool(value)


def _email_domain_valid(email: str | None, approved_domains: list | None) -> bool:
    """Return True if the domain part of *email* is present in *approved_domains*.

    Comparison is case-insensitive and both sides are stripped of whitespace to
    handle dirty API data (e.g. trailing spaces in domain entries).
    Returns False for any None / empty input.
    """
    if not email or not approved_domains:
        return False
    parts = str(email).strip().rsplit("@", 1)
    if len(parts) != 2:
        return False
    user_domain = parts[1].strip().lower()
    normalised = [d.strip().lower() for d in approved_domains if isinstance(d, str)]
    return user_domain in normalised


def _compare_enrollment_vs_admin_state_helper(
    lang_content_status: Any,
    admin_content_states: Any,
) -> bool:
    """Thin wrapper so branch expressions can call compare_enrollment_vs_admin_state().

    Delegates to the canonical implementation in api_call_node.py to avoid
    duplicating the cross-comparison logic.
    """
    from app.engine.nodes.api_call_node import _compare_enrollment_vs_admin_state
    return _compare_enrollment_vs_admin_state(lang_content_status, admin_content_states)


def _extract_incomplete_child_courses(cap_hierarchy_children: Any, all_enrollment_list: Any) -> list[dict]:
    """Thin wrapper so branch expressions can call extract_incomplete_child_courses()."""
    from app.engine.nodes.api_call_node import _extract_incomplete_child_courses as _impl
    return _impl(cap_hierarchy_children, all_enrollment_list)


def _check_cap_technical_issue(all_enrollments: Any, admin_states: Any, course_id: Any) -> bool:
    """Thin wrapper so branch expressions can call check_cap_technical_issue()."""
    from app.engine.nodes.api_call_node import _check_cap_technical_issue as _impl
    return _impl(all_enrollments, admin_states, course_id)


HELPERS = {
    "hours_since": _hours_since,
    "days_since": _days_since,
    "has": _has,
    "email_domain_valid": _email_domain_valid,
    # Admin Content State vs Enrollment API cross-comparison
    # Used in branch rules: compare_enrollment_vs_admin_state(lang_content_status, admin_content_states)
    # Returns True if any leaf resource has enrollment status=1 (In-Progress) AND admin status=2 (Completed)
    "compare_enrollment_vs_admin_state": _compare_enrollment_vs_admin_state_helper,
    "check_cap_technical_issue": _check_cap_technical_issue,
    "extract_incomplete_child_courses": _extract_incomplete_child_courses,
    "len": len,
    "str": str,
    "int": int,
    "float": float,
    "lower": str.lower,
    "upper": str.upper,
}


class ExpressionEvaluator:
    """Sandboxed expression evaluator over a context dict."""

    def __init__(self) -> None:
        self.evaluator = EvalWithCompoundTypes(functions=HELPERS)

    def evaluate(self, expression: str, ctx: dict[str, Any]) -> Any:
        """Evaluate `expression` with `ctx` available as `ctx.<key>`.

        Convention: expressions reference state as `ctx.collected.foo`,
        `ctx.counters.dissatisfaction_count`, etc.
        """
        self.evaluator.names = {"ctx": _DotDict(ctx)}
        try:
            return self.evaluator.eval(expression)
        except Exception as e:  # noqa: BLE001
            raise ExpressionError(
                f"Expression evaluation failed: {expression!r} — {e}"
            ) from e


class _DotDict:
    """Wraps a dict to allow attribute-style access for expressions."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getattr__(self, key: str) -> Any:
        if key in self._data:
            value = self._data[key]
            if isinstance(value, dict):
                return _DotDict(value)
            return value
        # Return None for missing keys rather than raising — flow YAMLs
        # often have conditional branches checking optional fields
        return None

    def __getitem__(self, key: str) -> Any:
        return self.__getattr__(key)


class ExpressionError(Exception):
    """Raised when a YAML expression fails to evaluate."""
