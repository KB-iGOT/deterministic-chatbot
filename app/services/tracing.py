"""Langfuse observability tracing service.

Toggle:
    LANGFUSE_ENABLED=true   +  LANGFUSE_PUBLIC_KEY  +  LANGFUSE_SECRET_KEY  in .env

When disabled (default), all context managers are no-ops — zero overhead, no imports.

Trace model
-----------
One Langfuse *trace* per HTTP turn (session-start or user-turn).
Every trace carries:
  - user_id   = HMAC-hashed user UUID (same as ctx.user_id in dev, hashed in prod)
  - session_id = the chat session UUID
  - trace_name = e.g. "session-start" / "turn-CERTIFICATE_DOWNLOAD"

Langfuse's Sessions view automatically groups all traces that share a session_id
into a single conversation thread — no extra work needed.

Within turns that trigger an LLM call, a 'generation' child span is added so you
can see model, input length, output, and latency in the LLM calls tab.

Usage (routes.py):
    async with tracing.turn_trace(
        user_id=user_id_hash,
        session_id=sid,
        trace_name="turn-CERTIFICATE_DOWNLOAD",
        flow_id="CERTIFICATE_DOWNLOAD",
        channel="web",
    ):
        result = await graph.ainvoke(...)
        tracing.set_trace_output(status=result_status, activities=len(activities))

Usage (llm.py):
    async with tracing.generation_span(model="gemini-2.5-flash", operation="ticket_summary"):
        raw = await self._call(prompt)
        tracing.update_current_generation(output_summary=raw[:500])
"""

from __future__ import annotations

import logging
from contextlib import contextmanager, nullcontext
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


# ── Turn-level context manager ─────────────────────────────────────────────────

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

    Nests two OTel contexts:
      1. propagate_attributes — sets user_id, session_id, trace_name, tags
         so Langfuse groups all traces for a session under "Sessions" view.
      2. start_as_current_observation(as_type="chain") — creates the root span
         that makes the trace visible and enables set_trace_io / generation children.

    Returns a no-op when tracing is disabled — zero overhead, zero imports.

    Example::

        with tracing.turn_trace(
            user_id=user_id_hash,
            session_id=sid,
            trace_name="turn-CERTIFICATE_DOWNLOAD",
            flow_id="CERTIFICATE_DOWNLOAD",
            channel="web",
        ):
            result = await graph.ainvoke(...)
            tracing.set_trace_io(input={...}, output={...})
    """
    if not _enabled or _client is None:
        yield
        return

    from langfuse import propagate_attributes

    with propagate_attributes(
        user_id=user_id,
        session_id=session_id,
        trace_name=trace_name,
        tags=tags or [],
        metadata={k: str(v) for k, v in metadata.items() if v is not None},
    ):
        with _client.start_as_current_observation(
            name=trace_name,
            as_type="chain",
            metadata={"user_id": user_id, "session_id": session_id, **{k: str(v) for k, v in metadata.items() if v is not None}},
        ):
            yield


def set_trace_io(
    *,
    input: dict[str, Any] | None = None,
    output: dict[str, Any] | None = None,
) -> None:
    """Update the current trace's top-level input/output shown in the Langfuse UI.

    Call after awaiting graph.ainvoke() to add a clean summary to the trace.
    No-op when tracing is disabled.
    """
    if not _enabled or _client is None:
        return
    try:
        _client.set_current_trace_io(input=input, output=output)
    except Exception as exc:  # noqa: BLE001
        log.debug("[tracing] set_trace_io failed: %s", exc)


# ── LLM generation span ────────────────────────────────────────────────────────

@contextmanager
def generation_span(*, model: str, operation: str, prompt_len: int = 0):
    """Context manager: record one LLM call as a Langfuse generation child span.

    Must be nested inside a turn_trace context so Langfuse nests it under the
    correct trace (with user_id + session_id).
    Prompt text is NOT logged (PII) — only the character length.
    Call update_current_generation() inside the block to set the output.

    Returns no-op when tracing is disabled.

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

    with _client.start_as_current_observation(
        name=f"llm-{operation}",
        as_type="generation",
        model=model,
        metadata={"operation": operation, "prompt_chars": prompt_len},
    ):
        yield


def update_current_generation(
    *,
    output: str | None = None,
    usage_input: int | None = None,
    usage_output: int | None = None,
) -> None:
    """Update the LLM generation span currently on the stack with output text + token counts.

    output:        First N chars of LLM response (caller decides how much to keep).
    usage_input:   Approximate input token count (if available from SDK response).
    usage_output:  Approximate output token count.
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
