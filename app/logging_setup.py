"""Centralised logging configuration for iGOT Deterministic Chatbot.

Call ``configure_logging()`` once at application startup (in main.py lifespan).

Two output channels
-------------------
Console (stderr)
    Human-readable, colour-coded by level.  Always active.
    Format:  ``2026-06-03 10:30:15.123  INFO     zoho            Token refreshed. expires_in=3600s``

File (rotating)
    JSON-per-line (NDJSON).  Active only when ``LOG_FILE`` is set in the environment.
    Rotates at ``LOG_FILE_MAX_BYTES`` (default 10 MB), keeps ``LOG_FILE_BACKUP_COUNT``
    (default 5) compressed copies.
    JSON keys: ``ts``, ``level``, ``logger``, ``msg``, ``exc`` (optional).
    Easy to ship to Loki / CloudWatch / ELK without further parsing.

File location
-------------
Set ``LOG_FILE`` in ``.env`` to an absolute path, e.g.::

    LOG_FILE=/var/log/igot-chatbot/igot-chatbot.log

Or a relative path (resolved from the project root), e.g.::

    LOG_FILE=logs/igot-chatbot.log

The parent directory is created automatically.

Level conventions
-----------------
DEBUG   — internal state: token reuse, Jinja render output, node transitions
INFO    — normal operations: token refresh, ticket raised, flow loaded, HTTP 200
WARNING — unexpected but recoverable: 401 forced refresh, stub mode, env var missing
ERROR   — failure needing attention: HTTP error, ticket failed, render error
CRITICAL— app cannot start / serve requests
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Any

# ── ANSI colour codes ─────────────────────────────────────────────────────────
_RESET = "\033[0m"
_BOLD  = "\033[1m"
_DIM   = "\033[2m"
_LEVEL_COLOURS = {
    "DEBUG":    "\033[37m",        # white
    "INFO":     "\033[32m",        # green
    "WARNING":  "\033[33m",        # yellow
    "ERROR":    "\033[31m",        # red
    "CRITICAL": "\033[1m\033[31m", # bold red
}
_CYAN = "\033[36m"

# Logger name → short alias for compact terminal output
_NAME_ALIASES: dict[str, str] = {
    "app.adapters.zoho":                  "zoho",
    "app.engine.nodes.api_call_node":     "api_call",
    "app.engine.nodes.message_node":      "msg_node",
    "app.engine.nodes.transfer_llm_node": "llm_node",
    "app.engine.compiler":                "compiler",
    "app.engine.runner":                  "runner",
    "app.api.routes":                     "routes",
    "app.services.karmayogi":             "karmayogi",
    "app.services.registry":              "registry",
    "app.session":                        "session",
    "uvicorn.access":                     "http",
    "uvicorn.error":                      "uvicorn",
}


# ── Console formatter ─────────────────────────────────────────────────────────

class _ConsoleFormatter(logging.Formatter):
    """Human-readable colour-coded formatter for the terminal."""

    _tty: bool | None = None

    @classmethod
    def _is_tty(cls) -> bool:
        if cls._tty is None:
            cls._tty = hasattr(sys.stderr, "isatty") and sys.stderr.isatty()
        return cls._tty

    def format(self, record: logging.LogRecord) -> str:
        col = self._is_tty()

        ts  = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        ms  = f"{record.msecs:03.0f}"
        timestamp = f"{_DIM}{ts}.{ms}{_RESET}" if col else f"{ts}.{ms}"

        level = record.levelname.ljust(8)
        if col:
            level = f"{_LEVEL_COLOURS.get(record.levelname, '')}{level}{_RESET}"

        raw  = record.name
        name = _NAME_ALIASES.get(raw) or ".".join(raw.split(".")[-2:])
        name = name.ljust(16)
        if col:
            name = f"{_CYAN}{name}{_RESET}"

        msg = record.getMessage()
        exc = ""
        if record.exc_info:
            exc = "\n" + self.formatException(record.exc_info)

        return f"{timestamp}  {level}  {name}  {msg}{exc}"


# ── File formatter (NDJSON) ───────────────────────────────────────────────────

class _JsonFormatter(logging.Formatter):
    """One JSON object per line — easy to ingest into any log aggregator."""

    def format(self, record: logging.LogRecord) -> str:
        raw = record.name
        doc: dict[str, Any] = {
            "ts":     self.formatTime(record, "%Y-%m-%dT%H:%M:%S") + f".{record.msecs:03.0f}Z",
            "level":  record.levelname,
            "logger": _NAME_ALIASES.get(raw, raw),
            "msg":    record.getMessage(),
        }
        if record.exc_info:
            doc["exc"] = self.formatException(record.exc_info)
        return json.dumps(doc, ensure_ascii=False)


# ── Public API ────────────────────────────────────────────────────────────────

def configure_logging(level: str = "INFO", log_file: str = "") -> None:
    """Configure all loggers for the iGOT Deterministic Chatbot application.

    Parameters
    ----------
    level:
        Log level for iGOT Deterministic Chatbot loggers (DEBUG / INFO / WARNING / ERROR).
    log_file:
        Optional file path for rotating JSON log file.
        If empty, file logging is disabled.
        Parent directory is created automatically.
    """
    numeric = getattr(logging, level.upper(), logging.INFO)

    # ── Console handler ───────────────────────────────────────────────────────
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(_ConsoleFormatter())
    console.setLevel(logging.DEBUG)

    handlers: list[logging.Handler] = [console]

    # ── File handler (rotating, JSON) ─────────────────────────────────────────
    _file_handler: logging.handlers.RotatingFileHandler | None = None
    if log_file:
        log_path = Path(log_file)
        if not log_path.is_absolute():
            # Resolve relative paths from the project root (two levels up from this file)
            log_path = Path(__file__).resolve().parent.parent / log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)

        # Import settings only if needed (avoids circular import at module level)
        try:
            from app.config import settings as _s
            max_bytes    = _s.log_file_max_bytes
            backup_count = _s.log_file_backup_count
        except Exception:
            max_bytes    = 10 * 1024 * 1024
            backup_count = 5

        _file_handler = logging.handlers.RotatingFileHandler(
            filename     = log_path,
            maxBytes     = max_bytes,
            backupCount  = backup_count,
            encoding     = "utf-8",
        )
        _file_handler.setFormatter(_JsonFormatter())
        _file_handler.setLevel(logging.DEBUG)
        handlers.append(_file_handler)

    # ── Root logger ───────────────────────────────────────────────────────────
    root = logging.getLogger()
    root.handlers.clear()
    for h in handlers:
        root.addHandler(h)
    root.setLevel(numeric)

    # ── iGOT Deterministic Chatbot app namespaces ─────────────────────────────────────────────────
    for ns in ("app", "app.adapters", "app.engine", "app.api", "app.services"):
        logging.getLogger(ns).setLevel(numeric)

    # ── Third-party noise reduction ───────────────────────────────────────────
    for noisy in (
        "httpx", "httpcore", "hpack", "h2", "asyncio", "multipart",
        "langchain", "langgraph", "openai", "google", "vertexai",
        "grpc", "urllib3", "charset_normalizer", "anthropic",
        "watchfiles",        # uvicorn --reload file watcher — noisy at DEBUG
        "watchgod",          # older watchgod-based watcher
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # uvicorn: keep access log at INFO so requests appear in terminal
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)

    log = logging.getLogger("app")
    if log_file and _file_handler:
        log.info(
            "Logging configured. level=%s  console=stderr  file=%s  max=%dMB  keep=%d",
            level.upper(), log_path, max_bytes // (1024 * 1024), backup_count,
        )
    else:
        log.info(
            "Logging configured. level=%s  console=stderr  file=disabled",
            level.upper(),
        )


# ── Session context helper ────────────────────────────────────────────────────

class SessionLogger:
    """Prefixes every log line with session/flow/node context.

    Usage::

        slog = SessionLogger(session_id="abc123", flow_id="LOGIN_ISSUE")
        slog.info("User selected option", node="ask_login_issue")
        slog.error("Zoho ticket failed", node="confirm_ticket", error=str(e))

    Output::

        INFO  session  session=abc123  flow=LOGIN_ISSUE  User selected option  node=ask_login_issue
    """

    def __init__(
        self,
        session_id: str = "",
        flow_id:    str = "",
        logger_name: str = "app.session",
    ) -> None:
        self._log    = logging.getLogger(logger_name)
        self._prefix = "  ".join(p for p in [
            f"session={session_id}" if session_id else "",
            f"flow={flow_id}"       if flow_id    else "",
        ] if p)

    def _fmt(self, msg: str, **kw: Any) -> str:
        parts = [self._prefix, msg]
        if kw:
            parts.append("  ".join(f"{k}={v}" for k, v in kw.items()))
        return "  ".join(p for p in parts if p)

    def debug(self,    msg: str, **kw: Any) -> None: self._log.debug(self._fmt(msg, **kw))
    def info(self,     msg: str, **kw: Any) -> None: self._log.info(self._fmt(msg, **kw))
    def warning(self,  msg: str, **kw: Any) -> None: self._log.warning(self._fmt(msg, **kw))
    def error(self,    msg: str, **kw: Any) -> None: self._log.error(self._fmt(msg, **kw))
    def critical(self, msg: str, **kw: Any) -> None: self._log.critical(self._fmt(msg, **kw))
