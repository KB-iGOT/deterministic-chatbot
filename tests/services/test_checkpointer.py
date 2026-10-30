"""Checkpointer pool bootstrap, readiness, and recovery tests.

Unit tests mock psycopg_pool; the integration tests need a real Postgres and run
only when TEST_POSTGRES_URL is set (e.g. postgresql://user:pw@localhost:5432/db).
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.services import checkpointer as cp


def _settings(**kw) -> Settings:
    # No startup retries by default so failure-path tests stay instant.
    kw.setdefault("postgres_startup_retry_seconds", 0)
    return Settings(_env_file=None, **kw)


# Import before any patching so langgraph binds the real AsyncConnectionPool class.
import langgraph.checkpoint.postgres.aio  # noqa: E402,F401


def _patch_pool(monkeypatch, *, open_exc=None, setup_exc=None):
    pool = MagicMock()
    pool.open = AsyncMock(side_effect=open_exc)
    pool.close = AsyncMock()
    pool.get_stats = MagicMock(return_value={})
    ctor = MagicMock(return_value=pool)
    ctor.check_connection = object()
    monkeypatch.setattr("psycopg_pool.AsyncConnectionPool", ctor)
    saver = MagicMock()
    saver.setup = AsyncMock(side_effect=setup_exc)
    monkeypatch.setattr(
        "langgraph.checkpoint.postgres.aio.AsyncPostgresSaver", MagicMock(return_value=saver)
    )
    return pool, ctor, saver


async def test_pool_init_and_setup(monkeypatch):
    pool, ctor, saver = _patch_pool(monkeypatch)
    h = await cp.init_checkpointer(_settings())
    assert h.kind == "postgres" and h.checkpointer is saver and h.pool is pool
    kw = ctor.call_args.kwargs
    assert kw["check"] is ctor.check_connection
    assert kw["kwargs"]["autocommit"] is True and kw["kwargs"]["prepare_threshold"] == 0
    assert kw["conninfo"].startswith("postgresql://")  # +asyncpg stripped
    pool.open.assert_awaited_once()
    saver.setup.assert_awaited_once()


async def test_shutdown_closes_pool(monkeypatch):
    pool, _, _ = _patch_pool(monkeypatch)
    h = await cp.init_checkpointer(_settings())
    await h.aclose()
    pool.close.assert_awaited_once()
    assert h.pool is None
    await h.aclose()  # idempotent
    pool.close.assert_awaited_once()


@pytest.mark.parametrize("stage", ["open_exc", "setup_exc"])
async def test_dev_falls_back_to_memory_and_closes_pool(monkeypatch, stage):
    pool, _, _ = _patch_pool(monkeypatch, **{stage: OSError("db down")})
    h = await cp.init_checkpointer(_settings(igot_env="dev"))
    assert h.kind == "memory" and h.pool is None
    pool.close.assert_awaited_once()


async def test_prod_refuses_memory_fallback(monkeypatch):
    pool, _, _ = _patch_pool(monkeypatch, open_exc=OSError("db down"))
    with pytest.raises(RuntimeError, match="required but unavailable"):
        await cp.init_checkpointer(_settings(igot_env="prod"))
    pool.close.assert_awaited_once()


async def test_prod_fallback_allowed_when_explicitly_optional(monkeypatch):
    _patch_pool(monkeypatch, open_exc=OSError("db down"))
    h = await cp.init_checkpointer(_settings(igot_env="prod", postgres_required=False))
    assert h.kind == "memory"


async def test_failure_log_does_not_leak_password(monkeypatch, caplog):
    _patch_pool(monkeypatch, open_exc=OSError("db down"))
    s = _settings(postgres_url="postgresql+asyncpg://u:s3cretpw@h:5432/d")
    await cp.init_checkpointer(s)
    assert "s3cretpw" not in caplog.text


async def test_startup_retries_until_postgres_is_up(monkeypatch):
    pool, ctor, saver = _patch_pool(monkeypatch, open_exc=[OSError("down"), OSError("down"), None])
    h = await cp.init_checkpointer(
        _settings(igot_env="prod", postgres_startup_retry_seconds=5, postgres_startup_retry_interval=0.01)
    )
    assert h.kind == "postgres"
    assert pool.open.await_count == 3
    assert pool.close.await_count == 2  # each failed attempt closed its pool


async def test_startup_retry_exhausted_prod_raises(monkeypatch):
    pool, _, _ = _patch_pool(monkeypatch, open_exc=OSError("down"))
    with pytest.raises(RuntimeError, match="required but unavailable"):
        await cp.init_checkpointer(
            _settings(igot_env="prod", postgres_startup_retry_seconds=0.1, postgres_startup_retry_interval=0.02)
        )
    assert pool.open.await_count >= 2


async def test_startup_retry_exhausted_dev_falls_back(monkeypatch):
    pool, _, _ = _patch_pool(monkeypatch, open_exc=OSError("down"))
    h = await cp.init_checkpointer(
        _settings(igot_env="dev", postgres_startup_retry_seconds=0.1, postgres_startup_retry_interval=0.02)
    )
    assert h.kind == "memory" and pool.open.await_count >= 2


def test_postgres_required_defaults():
    assert _settings(igot_env="prod").postgres_is_required
    assert not _settings(igot_env="dev").postgres_is_required
    assert not _settings(igot_env="prod", postgres_required=False).postgres_is_required
    assert _settings(igot_env="dev", postgres_required=True).postgres_is_required


async def test_reconnect_failed_callback_logs_pool_stats(caplog):
    pool = MagicMock()
    pool.get_stats = MagicMock(return_value={"pool_size": 0})
    await cp._reconnect_failed(pool)
    assert "could not reconnect" in caplog.text and "pool_size" in caplog.text


# ── /ready ────────────────────────────────────────────────────────────────────

def _ready_client(monkeypatch, handle, *, required):
    import app.main as m

    monkeypatch.setattr(m.settings, "postgres_required", required)
    a = FastAPI()
    a.state.checkpointer_handle = handle
    a.add_api_route("/ready", m.ready, methods=["GET"])
    return TestClient(a)


class _Conn:
    def __init__(self, exc=None):
        self.exc = exc

    async def __aenter__(self):
        if self.exc:
            raise self.exc
        return SimpleNamespace(execute=AsyncMock())

    async def __aexit__(self, *a):
        return False


def _pg_handle(exc=None):
    pool = MagicMock()
    pool.connection = MagicMock(return_value=_Conn(exc))
    pool.get_stats = MagicMock(return_value={})
    return cp.CheckpointerHandle(checkpointer=MagicMock(), pool=pool, kind="postgres")


def _memory_handle():
    return cp.CheckpointerHandle(checkpointer=MagicMock(), kind="memory")


def test_ready_ok_with_postgres(monkeypatch):
    r = _ready_client(monkeypatch, _pg_handle(), required=True).get("/ready")
    assert r.status_code == 200 and r.json()["checkpointer"] == "postgres"


def test_ready_503_when_postgres_check_fails(monkeypatch):
    r = _ready_client(monkeypatch, _pg_handle(OSError("x")), required=True).get("/ready")
    assert r.status_code == 503


def test_ready_503_on_memory_when_required(monkeypatch):
    h = _memory_handle()
    assert _ready_client(monkeypatch, h, required=True).get("/ready").status_code == 503


def test_ready_200_on_memory_when_optional_and_flagged(monkeypatch):
    h = _memory_handle()
    r = _ready_client(monkeypatch, h, required=False).get("/ready")
    assert r.status_code == 200 and r.json() == {
        "status": "ready", "checkpointer": "memory", "persistent": False,
    }


# ── Integration (real Postgres) ───────────────────────────────────────────────

PG = os.environ.get("TEST_POSTGRES_URL")
needs_pg = pytest.mark.skipif(not PG, reason="TEST_POSTGRES_URL not set")


def _cfg(**kw):
    return _settings(postgres_url=PG, postgres_pool_min_size=1, postgres_pool_max_size=4, **kw)


def _ckpt(thread, n):
    return (
        {"configurable": {"thread_id": thread, "checkpoint_ns": ""}},
        {"v": 1, "id": f"{n:032d}", "ts": "2026-01-01T00:00:00+00:00",
         "channel_values": {"k": thread}, "channel_versions": {}, "versions_seen": {},
         "pending_sends": []},
    )


@needs_pg
async def test_integration_recovers_after_connections_killed():
    h = await cp.init_checkpointer(_cfg(igot_env="prod"))
    try:
        assert h.kind == "postgres"
        cfg = {"configurable": {"thread_id": "t-recover", "checkpoint_ns": ""}}
        assert await h.checkpointer.aget_tuple(cfg) is None
        # Kill every other backend on this DB → pooled connections are now dead.
        async with h.pool.connection() as c:
            await c.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid()"
            )
        await asyncio.sleep(0.5)
        # check_connection must discard dead connections; the next call succeeds.
        await h.checkpointer.aget_tuple(cfg)
    finally:
        await h.aclose()


@needs_pg
async def test_integration_pool_timeout_when_exhausted():
    from psycopg_pool import PoolTimeout

    h = await cp.init_checkpointer(_cfg(igot_env="prod", postgres_pool_timeout=1))
    held = []
    try:
        for _ in range(4):
            cm = h.pool.connection()
            await cm.__aenter__()
            held.append(cm)
        with pytest.raises(PoolTimeout):
            async with h.pool.connection(timeout=0.5):
                pass
    finally:
        for cm in held:
            await cm.__aexit__(None, None, None)
        await h.aclose()


@needs_pg
async def test_integration_concurrent_threads_isolated():
    h = await cp.init_checkpointer(_cfg(igot_env="prod"))
    try:
        threads = [f"t-iso-{i}" for i in range(8)]

        async def put(t):
            cfg, ck = _ckpt(t, 1)
            await h.checkpointer.aput(cfg, ck, {}, {})

        await asyncio.gather(*(put(t) for t in threads))
        for t in threads:
            got = await h.checkpointer.aget_tuple(
                {"configurable": {"thread_id": t, "checkpoint_ns": ""}}
            )
            assert got.checkpoint["channel_values"] == {"k": t}
    finally:
        await h.aclose()
