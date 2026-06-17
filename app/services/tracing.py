"""Langfuse observability tracing service.

Toggle:
    LANGFUSE_ENABLED=true   +  LANGFUSE_PUBLIC_KEY  +  LANGFUSE_SECRET_KEY  in .env

When disabled (default), all functions are no-ops — zero overhead, zero imports.

Langfuse v3 trace model (SDK 4.x)
------------------------------------
One Langfuse *trace* per HTTP turn, created via:

    with propagate_attributes(user_id=..., session_id=..., trace_name=...):
        with _client.start_as_current_observation(name=..., as_type="chain"):
            yield  # route handler runs here

`propagate_attributes` injects user_id, session_id, and tags into the OTel context.
The span processor picks these up when creating the trace, so all traces sharing the
same session_id are grouped in the Langfuse Sessions view automatically.

Within turns that trigger an LLM call, `start_as_current_observation(as_type="generation")`
adds a child generation span so you can see model, token usage, and latency.

What each session produces in Langfuse
---------------------------------------
  session-start          → 1 trace  (greeting shown, menu served)
  flow-start-{flow_id}   → 1 trace  (user picked a topic)
  turn-{flow_id}         → N traces (one per user action inside the flow)
  session-end            → 1 trace  (terminal state: outcome + ticket_id + node_path)

Filter "session-end" traces by tag to answer:
  - "ticket_raised"        → all sessions that created a Zoho ticket
  - "self_served"          → all self-resolved sessions
  - "CERTIFICATE_DOWNLOAD" → all cert-download sessions, any outcome
  - user_id = X            → every session for user X in any timeframe
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any

log = logging.getLogger(__name__)

_client: Any = None   # Langfuse instance (None when disabled)
_enabled: bool = False


# ── Lifecycle ──────────────────────────────────────────────────────────────────

def init() -> None:
    """Initialise the Langfuse client. Call once in the FastAPI lifespan (startup)."""
    global _client, _enabled
    from app.config import settings

    if not settings.langfuse_enabled:
        log.debug("[tracing] Langfuse disabled (LANGFUSE_ENABLED=false)")
        return

    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        log.warning(
            "[tracing] LANGFUSE_ENABLED=true but keys not set "
            "(LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY) — tracing disabled"
        )
        return

    try:
        from langfuse import Langfuse

        host = settings.langfuse_host or "https://cloud.langfuse.com"
        _client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=host,
            sample_rate=settings.langfuse_sample_rate,
        )
        _enabled = True
        log.info(
            "[tracing] Langfuse enabled. host=%s  sample_rate=%s",
            host, settings.langfuse_sample_rate,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("[tracing] Failed to init Langfuse — tracing disabled. error=%s", exc)


def shutdown() -> None:
    """Flush pending spans to Langfuse. Call from lifespan teardown."""
    if _enabled and _client is not None:
        try:
            _client.flush()
            log.debug("[tracing] Langfuse flushed on shutdown")
        except Exception as exc:  # noqa: BLE001
            log.warning("[tracing] Langfuse flush error: %s", exc)


def is_enabled() -> bool:
    return _enabled


# ── Turn-level trace ───────────────────────────────────────────────────────────

@contextmanager
def turn_trace(
    *,
    user_id: str,
    session_id: str,
    trace_name: str,
    tags: list[str] | None = None,
    **metadata: Any,
):
    """Context manager: open a Langfuse trace for one HTTP turn.

    Uses propagate_attributes (Langfuse 4.x SDK) to inject user_id, session_id,
    and tags into the OTel context before creating the root observation span.
    The span processor picks these up so all traces with the same session_id are
    grouped in the Langfuse Sessions view.

    No-op when tracing is disabled — zero overhead, zero imports.

    Example::

        with tracing.turn_trace(
            user_id=user_id,
            session_id=sid,
            trace_name="turn-CERTIFICATE_DOWNLOAD",
            tags=["web", "en", "CERTIFICATE_DOWNLOAD"],
            flow_id="CERTIFICATE_DOWNLOAD",
            channel="web",
        ):
            result = await graph.ainvoke(...)
            tracing.set_trace_io(input={...}, output={...})
    """
    if not _enabled or _client is None:
        yield
        return

    _meta = {k: str(v) for k, v in metadata.items() if v is not None}

    try:
        from langfuse import propagate_attributes
        with propagate_attributes(
            user_id=user_id,
            session_id=session_id,
            trace_name=trace_name,
            tags=tags or [],
            metadata=_meta,
        ):
            with _client.start_as_current_observation(
                name=trace_name,
                as_type="chain",
                metadata=_meta,
            ):
                yield
    except Exception as exc:  # noqa: BLE001
        log.warning("[tracing] turn_trace error: %s", exc)
        yield


def set_trace_io(
    *,
    input: dict[str, Any] | None = None,
    output: dict[str, Any] | None = None,
) -> None:
    """Update the current trace's input/output shown in the Langfuse UI.

    Call after awaiting graph.ainvoke() to add a clean summary to the trace.
    No-op when tracing is disabled.
    """
    if not _enabled or _client is None:
        return
    try:
        _client.set_current_trace_io(input=input, output=output)
    except Exception as exc:  # noqa: BLE001
        log.debug("[tracing] set_trace_io failed: %s", exc)


# ── Session end summary ────────────────────────────────────────────────────────

def record_session_end(
    *,
    user_id: str,
    session_id: str,
    flow_id: str | None,
    outcome: str,
    ticket_id: str | None = None,
    turn_count: int = 0,
    node_path: list[str] | None = None,
    channel: str = "web",
    language: str = "en",
) -> None:
    """Create a terminal summary trace for a completed session.

    This trace shares session_id with all turn traces so Langfuse groups them
    together in the Sessions view.  Tags include outcome and flow_id so you can
    pivot on them in dashboards:

      Filter "session-end" traces by tag "ticket_raised"        → ticket count per flow
      Filter "session-end" traces by tag "CERTIFICATE_DOWNLOAD" → cert flow outcomes
      Filter by user_id                                          → all sessions for a user

    No-op when tracing is disabled.
    """
    if not _enabled or _client is None:
        return
    try:
        from langfuse import propagate_attributes
        _path_str = " → ".join(node_path) if node_path else ""
        _tags = [channel, language, flow_id or "no_flow", outcome]
        _input = {
            "flow_id": flow_id,
            "turn_count": turn_count,
            "node_path": node_path or [],
        }
        _output = {
            "outcome": outcome,
            "ticket_id": ticket_id,
            "ticket_raised": ticket_id is not None,
            "self_served": outcome == "self_served",
        }
        _meta = {
            "user_id": user_id,
            "session_id": session_id,
            "flow_id": flow_id or "",
            "outcome": outcome,
            "ticket_id": ticket_id or "",
            "turn_count": str(turn_count),
            "node_path": _path_str,
            "channel": channel,
            "language": language,
        }

        with propagate_attributes(
            user_id=user_id,
            session_id=session_id,
            trace_name="session-end",
            tags=_tags,
            metadata=_meta,
        ):
            with _client.start_as_current_observation(
                name="session-end",
                as_type="chain",
                input=_input,
                output=_output,
                metadata=_meta,
            ):
                pass
    except Exception as exc:  # noqa: BLE001
        log.debug("[tracing] record_session_end failed: %s", exc)


# ── LLM generation span ────────────────────────────────────────────────────────

@contextmanager
def generation_span(*, model: str, operation: str, prompt_len: int = 0):
    """Context manager: record one LLM call as a Langfuse generation child span.

    Must be nested inside a turn_trace context so Langfuse nests it under the
    correct trace (with user_id + session_id already set on the parent trace).
    Prompt text is NOT logged (PII) — only the character length.
    Call update_current_generation() inside the block to set the output.

    No-op when tracing is disabled.

    Example::

        with tracing.generation_span(model="gemini-2.5-flash",
                                     operation="ticket_summary",
                                     prompt_len=len(prompt)):
            raw = await self._call(prompt)
            tracing.update_current_generation(output=raw[:500])
    """
    if not _enabled or _client is None:
        yield
        return

    try:
        with _client.start_as_current_observation(
            name=f"llm-{operation}",
            as_type="generation",
            model=model,
            metadata={"operation": operation, "prompt_chars": prompt_len},
        ):
            yield
    except Exception as exc:  # noqa: BLE001
        log.warning("[tracing] generation_span error: %s", exc)
        yield


def update_current_generation(
    *,
    output: str | None = None,
    usage_input: int | None = None,
    usage_output: int | None = None,
) -> None:
    """Update the active LLM generation span with output text and token counts.

    output:        First N chars of the LLM response (caller trims for size).
    usage_input:   Input token count (from SDK response if available).
    usage_output:  Output token count.
    No-op when tracing is disabled.
    """
    if not _enabled or _client is None:
        return
    try:
        kwargs: dict[str, Any] = {}
        if output is not None:
            kwargs["output"] = output
        if usage_input is not None or usage_output is not None:
            kwargs["usage_details"] = {
                k: v for k, v in [("input", usage_input), ("output", usage_output)]
                if v is not None
            }
        if kwargs:
            _client.update_current_generation(**kwargs)
    except Exception as exc:  # noqa: BLE001
        log.debug("[tracing] update_current_generation failed: %s", exc)
