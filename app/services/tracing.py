"""Langfuse observability tracing service.

Toggle:
    LANGFUSE_ENABLED=true   +  LANGFUSE_PUBLIC_KEY  +  LANGFUSE_SECRET_KEY  in .env

When disabled (default), all functions are no-ops — zero overhead, zero imports.

Langfuse v4 trace model (SDK 4.x)
-----------------------------------
Each HTTP turn produces one Langfuse OBSERVATION (chain-type span).  All turns in
the same chat session are chained into a single Langfuse TRACE via trace_id /
parent_observation_id so the full session appears as one timeline in the UI.

Trace chain per session:
  session-start              (root span — no parent)
    └── flow-start-{flow_id} (child of session-start)
          └── turn-{flow_id} (child of flow-start, then child of previous turn)
                └── ...
                      └── session-end (child of the last turn)

To maintain the chain:
  • routes.py calls tracing.get_current_span_ids() INSIDE each turn_trace block
    to capture (trace_id, observation_id) of the span just created.
  • Those IDs are stored in the in-memory session dict.
  • The NEXT turn passes them as trace_id / parent_observation_id so the new span
    becomes a child of the previous one inside the same OTel trace.

Result in Langfuse UI
  • Traces view  → one trace per session, expanding shows all turns as child spans.
  • Sessions view → sessions grouped by session_id (backup if trace-chain breaks).
  • Users view   → filter by user_id to see all sessions for a user.

LLM generation spans
  generation_span() creates a child "generation" span inside any turn_trace.
  Call update_current_generation() inside it to record model + token counts.

Disable
  LANGFUSE_ENABLED=false (or missing keys) → every function is a zero-cost no-op.
  The disable path never imports Langfuse so cold-start time is unaffected.
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


# ── Span ID helpers (used by routes to chain turns into one trace) ─────────────

def get_current_span_ids() -> tuple[str | None, str | None]:
    """Return (trace_id, observation_id) of the currently active Langfuse span.

    Must be called INSIDE a turn_trace block.  Returns (None, None) when tracing
    is disabled or no span is active.

    trace_id       — 32-char hex; pass as trace_id to the NEXT turn_trace call.
    observation_id — 16-char hex; pass as parent_observation_id to the NEXT call.
    """
    if not _enabled or _client is None:
        return None, None
    try:
        return _client.get_current_trace_id(), _client.get_current_observation_id()
    except Exception:  # noqa: BLE001
        return None, None


# ── Turn-level trace ───────────────────────────────────────────────────────────

@contextmanager
def turn_trace(
    *,
    user_id: str,
    session_id: str,
    trace_name: str,
    tags: list[str] | None = None,
    trace_id: str | None = None,
    parent_observation_id: str | None = None,
    **metadata: Any,
):
    """Context manager: open a Langfuse span for one HTTP turn.

    Ordering (Langfuse 4.x docs):
      1. start_as_current_observation — creates the root span for this turn.
      2. propagate_attributes         — sets user_id / session_id / tags directly
                                        on the just-created root span AND injects
                                        them into the OTel context so child spans
                                        (generation_span, LangChain callbacks, …)
                                        inherit them automatically.

    Session chaining — all turns in a session share ONE Langfuse trace:
      Pass trace_id + parent_observation_id (obtained via get_current_span_ids()
      during the PREVIOUS turn) so this turn's span becomes a child of the last.
      On the first turn (session-start) leave both as None → Langfuse creates a
      new root trace automatically.

    Exception safety:
      Exceptions raised inside the block propagate normally (span is marked ERROR,
      caller handles the HTTP 500).  Only Langfuse *setup* errors are caught and
      logged so tracing failures never break the user-facing request.

    No-op when tracing is disabled — zero overhead, zero imports.

    Example::

        # First turn (session-start) — no prior IDs
        with tracing.turn_trace(user_id=uid, session_id=sid, trace_name="session-start"):
            tid, oid = tracing.get_current_span_ids()
            ...
        session["_lf_trace_id"]  = tid
        session["_lf_obs_id"]    = oid

        # Subsequent turn — chains onto the previous span
        with tracing.turn_trace(
            user_id=uid, session_id=sid, trace_name="turn-FLOW",
            trace_id=session["_lf_trace_id"],
            parent_observation_id=session["_lf_obs_id"],
        ):
            tid, oid = tracing.get_current_span_ids()
            ...
        session["_lf_obs_id"] = oid   # trace_id never changes within a session
    """
    if not _enabled or _client is None:
        yield
        return

    _meta = {k: str(v) for k, v in metadata.items() if v is not None}
    _user_code_started = False

    try:
        from langfuse import propagate_attributes
        from langfuse.types import TraceContext

        _trace_ctx: TraceContext | None = None
        if trace_id:
            _trace_ctx = TraceContext(trace_id=trace_id)
            if parent_observation_id:
                _trace_ctx["parent_span_id"] = parent_observation_id

        with _client.start_as_current_observation(
            name=trace_name,
            as_type="chain",
            metadata=_meta,
            trace_context=_trace_ctx,
        ):
            with propagate_attributes(
                user_id=user_id,
                session_id=session_id,
                trace_name=trace_name,
                tags=tags or [],
                metadata=_meta,
            ):
                _user_code_started = True
                yield

    except Exception as exc:  # noqa: BLE001
        if _user_code_started:
            raise   # user code failed — propagate so routes.py returns HTTP 500
        log.warning("[tracing] turn_trace setup error: %s", exc)
        yield   # Langfuse setup failed — run user code without tracing


def set_span_io(
    *,
    input: Any | None = None,
    output: Any | None = None,
) -> None:
    """Set the current observation's input/output shown in the Langfuse timeline.

    Uses update_current_span (sets langfuse.observation.input / .output) — the
    non-deprecated approach that Langfuse's UI actually renders.

    Call inside a turn_trace block.  Calling twice is fine — the second call
    overwrites the first, so call once before the graph with just input, then
    again after with both input and output.  No-op when tracing is disabled.
    """
    if not _enabled or _client is None:
        return
    try:
        kwargs: dict[str, Any] = {}
        if input is not None:
            kwargs["input"] = input
        if output is not None:
            kwargs["output"] = output
        if kwargs:
            _client.update_current_span(**kwargs)
    except Exception as exc:  # noqa: BLE001
        log.debug("[tracing] set_span_io failed: %s", exc)


# Keep old name as alias so any external callers don't break
set_trace_io = set_span_io


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
    trace_id: str | None = None,
    parent_observation_id: str | None = None,
) -> None:
    """Create a terminal summary span for a completed session.

    Appended to the same trace (via trace_id / parent_observation_id) so it
    appears as the final child in the session timeline.  Tags include outcome and
    flow_id for dashboard pivots:

      Filter "session-end" by tag "ticket_raised"        → sessions that raised a ticket
      Filter "session-end" by tag "CERTIFICATE_DOWNLOAD" → cert flow outcomes
      Filter by user_id                                   → all sessions for a user

    No-op when tracing is disabled.
    """
    if not _enabled or _client is None:
        return
    try:
        from langfuse import propagate_attributes
        from langfuse.types import TraceContext

        # Normalise outcome to a plain str — result.get("status") may return a
        # FlowStatus str-enum, which OTel rejects in tag lists.
        outcome = outcome if type(outcome) is str else getattr(outcome, "value", str(outcome))

        _path_str = " → ".join(node_path) if node_path else ""
        _tags = [channel, language, flow_id or "no_flow", outcome]
        if ticket_id:
            _tags.append("ticket_raised")
        if outcome == "self_served":
            _tags.append("self_served")

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

        _trace_ctx: TraceContext | None = None
        if trace_id:
            _trace_ctx = TraceContext(trace_id=trace_id)
            if parent_observation_id:
                _trace_ctx["parent_span_id"] = parent_observation_id

        with _client.start_as_current_observation(
            name="session-end",
            as_type="chain",
            input=_input,
            output=_output,
            metadata=_meta,
            trace_context=_trace_ctx,
        ):
            with propagate_attributes(
                user_id=user_id,
                session_id=session_id,
                trace_name="session-end",
                tags=_tags,
                metadata=_meta,
            ):
                pass

    except Exception as exc:  # noqa: BLE001
        log.debug("[tracing] record_session_end failed: %s", exc)


# ── LLM generation span ────────────────────────────────────────────────────────

@contextmanager
def generation_span(*, model: str, operation: str, prompt_len: int = 0):
    """Context manager: record one LLM call as a Langfuse generation child span.

    Must be called INSIDE a turn_trace block so Langfuse nests it under the
    correct trace (user_id + session_id already set on the parent span).
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

    _user_code_started = False
    try:
        with _client.start_as_current_observation(
            name=f"llm-{operation}",
            as_type="generation",
            model=model,
            metadata={"operation": operation, "prompt_chars": prompt_len},
        ):
            _user_code_started = True
            yield
    except Exception as exc:  # noqa: BLE001
        if _user_code_started:
            raise
        log.warning("[tracing] generation_span setup error: %s", exc)
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
