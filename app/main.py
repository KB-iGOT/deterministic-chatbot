"""iGOT Deterministic Chatbot FastAPI entry point."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.routes import router as chat_router
from app.config import settings
from app.engine.compiler import FlowCompiler
from app.logging_setup import configure_logging
from app.services import tracing
from app.services.registry import ServiceRegistry

# Initialise logging before anything else so all startup messages are captured.
configure_logging(level=settings.log_level, log_file=settings.log_file)

# ── Google Cloud credentials bootstrap ───────────────────────────────────────
# pydantic-settings reads GOOGLE_APPLICATION_CREDENTIALS into Settings but does
# NOT propagate it into os.environ.  google-auth, google-genai, and vertexai all
# look at os.environ directly, so we sync it here — once, at import time — so
# every GCP library in every thread finds the credentials automatically.
if settings.google_application_credentials:
    os.environ.setdefault(
        "GOOGLE_APPLICATION_CREDENTIALS", settings.google_application_credentials
    )


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN201
    # ── Startup ──────────────────────────────────────────────────────────────
    # Tracing (Langfuse) — must init before services so all LLM calls are captured.
    tracing.init()

    # Service registry (adapters: Karmayogi, Zoho, LLM, Presidio, Translation)
    services = ServiceRegistry.from_env()

    # Checkpointer — Postgres in dev/prod (persistent across restarts), fallback to in-memory.
    # AsyncPostgresSaver.setup() is idempotent — creates the 4 LangGraph checkpoint tables if absent.
    checkpointer: Any
    _pg_cm = None
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        # Build a plain asyncpg connection string from settings (strip the +asyncpg driver prefix)
        pg_dsn = settings.postgres_url.replace("postgresql+asyncpg://", "postgresql://")
        _pg_cm = AsyncPostgresSaver.from_conn_string(pg_dsn)
        checkpointer = await _pg_cm.__aenter__()
        await checkpointer.setup()
        print("✅ Checkpointer: AsyncPostgresSaver (Postgres)")
    except Exception as e:  # noqa: BLE001
        from langgraph.checkpoint.memory import MemorySaver
        print(f"⚠️  Postgres checkpointer unavailable ({e}), falling back to MemorySaver")
        checkpointer = MemorySaver()
        _pg_cm = None

    # Session store — Redis-backed user→session_id mapping for cross-device resume.
    # Falls back gracefully: GET /sessions/mine returns null when Redis is unavailable.
    _redis_client = None
    session_store = None
    try:
        import redis.asyncio as aioredis
        from app.services.session_store import SessionStore
        _redis_client = aioredis.from_url(
            settings.redis_url, decode_responses=False, socket_connect_timeout=3
        )
        await _redis_client.ping()
        session_store = SessionStore(_redis_client, settings.igot_redis_namespace)
        print("✅ Session store: Redis")
    except Exception as e:  # noqa: BLE001
        print(f"⚠️  Redis unavailable ({e}) — GET /sessions/mine will always return null")

    # Flow compiler — loads and validates all YAML flows at startup.
    # Server refuses to start if any flow fails compilation (fast fail).
    compiler = FlowCompiler(services=services)
    compiled: dict = {}
    flows_dir = settings.project_root / "flows"
    if flows_dir.is_dir():
        try:
            compiled = compiler.compile_directory(flows_dir, checkpointer=checkpointer)
            print(f"✅ Loaded {len(compiled)} flow(s): {sorted(compiled.keys())}")
        except Exception as e:  # noqa: BLE001
            print(f"⚠️  Flow compilation warning: {e}")

    # Channel adapters — instantiated once, shared across requests.
    # Handlers access via request.app.state.channel_adapters[channel_name].
    from app.adapters.channel.web import WebAdapter
    channel_adapters = {
        "web": WebAdapter(),
        "mobile": WebAdapter(),  # same JSON contract as web
        # "whatsapp": WhatsAppAdapter(...)  ← Phase 3
        # "voice": VoiceAdapter(...)        ← Phase 4
    }

    # System messages — bot persona & user-facing strings (editable without Python changes).
    from ruamel.yaml import YAML as _YAML
    _yaml = _YAML(typ="safe", pure=True)
    _sys_msg_path = flows_dir / "_shared" / "system_messages.yaml"
    system_messages: dict = {}
    if _sys_msg_path.exists():
        try:
            with _sys_msg_path.open(encoding="utf-8") as _f:
                system_messages = _yaml.load(_f) or {}
            print(f"✅ System messages loaded from {_sys_msg_path.name}")
        except Exception as _e:  # noqa: BLE001
            print(f"⚠️  Could not load system_messages.yaml ({_e}) — using built-in defaults")

    app.state.services = services
    app.state.compiler = compiler
    app.state.channel_adapters = channel_adapters
    app.state.graphs = compiled
    app.state.checkpointer = checkpointer
    app.state.system_messages = system_messages
    app.state.sessions: dict[str, dict] = {}  # in-memory session metadata
    app.state.session_store = session_store   # Redis-backed; None when Redis unavailable

    # Engineering tickets DB setup
    engineering_db = None
    try:
        from app.services.engineering_db import EngineeringDBService
        engineering_db = EngineeringDBService(settings.postgres_url)
        await engineering_db.setup()
        services["engineering_db"] = engineering_db
    except Exception as e:
        print(f"⚠️  EngineeringDBService unavailable ({e})")

    # Dev UI banner — printed after all startup tasks so the port is known.
    if settings.igot_env in ("dev", "staging"):
        _port = os.environ.get("PORT", "8000")
        print(f"🎨 Dev UI  →  http://localhost:{_port}/dev-ui")

    yield

    # ── Shutdown ──────────────────────────────────────────────────────────────
    tracing.shutdown()
    await services.aclose()
    if _pg_cm is not None:
        await _pg_cm.__aexit__(None, None, None)
    if _redis_client is not None:
        await _redis_client.aclose()
    if engineering_db is not None:
        await engineering_db.aclose()


app = FastAPI(
    title="iGOT Deterministic Chatbot — Karmayogi Bharat Support Assistant",
    version="0.1.0",
    description=(
        "Multi-channel hybrid support chatbot for iGOT Karmayogi Bharat. "
        "LangGraph + YAML deterministic-first; LLM only at L2 handover (Mode B)."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat_router)

# ── Dev UI (dev + staging only) ───────────────────────────────────────────────
# Single-file HTML/JS chat widget for testing flows locally.
# Served same-origin → no CORS config needed, JWT auth stub accepts any token.
# Access at: http://localhost:8000/dev-ui
if settings.igot_env in ("dev", "staging"):
    from fastapi.responses import HTMLResponse

    _dev_ui_path = Path(__file__).resolve().parent.parent / "dev_ui" / "index.html"

    if _dev_ui_path.exists():
        @app.get("/dev-ui", include_in_schema=False, response_class=HTMLResponse)
        async def dev_ui() -> HTMLResponse:
            return HTMLResponse(_dev_ui_path.read_text(encoding="utf-8"))



@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", tags=["meta"])
async def root() -> dict[str, str]:
    return {
        "service": "igot-deterministic-chatbot",
        "version": "0.1.0",
        "docs": "/docs",
    }
