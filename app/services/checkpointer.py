"""LangGraph checkpointer bootstrap: pooled Postgres with an optional MemorySaver fallback."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from app.config import Settings

log = logging.getLogger(__name__)


@dataclass
class CheckpointerHandle:
    checkpointer: Any
    pool: Any = None  # psycopg_pool.AsyncConnectionPool when kind == "postgres"
    kind: str = "memory"  # "postgres" | "memory"

    async def aclose(self) -> None:
        if self.pool is not None:
            log.info("Closing Postgres checkpointer pool")
            await self.pool.close()
            self.pool = None
            log.info("Postgres checkpointer pool closed")


async def _reconnect_failed(pool: Any) -> None:
    log.error("Postgres pool could not reconnect after repeated attempts: %s", pool.get_stats())


async def _connect_postgres(settings: Settings) -> CheckpointerHandle:
    """One attempt: open a Postgres pool and build an AsyncPostgresSaver on it.

    Closes the pool and re-raises on any failure so the caller can retry.
    """
    pool = None
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool

        # Plain libpq DSN (strip the +asyncpg driver prefix).
        dsn = settings.postgres_url.replace("postgresql+asyncpg://", "postgresql://")
        # A pool (not one shared connection) so a dropped connection only fails the
        # request that held it: `check` validates each connection on checkout and
        # replaces dead ones; max_idle/max_lifetime recycle before the server or
        # network closes them; TCP keepalives stop idle-connection reaping.
        # autocommit/prepare_threshold/row_factory are required by AsyncPostgresSaver.
        pool = AsyncConnectionPool(
            conninfo=dsn,
            min_size=settings.postgres_pool_min_size,
            max_size=settings.postgres_pool_max_size,
            max_idle=settings.postgres_pool_max_idle,
            max_lifetime=settings.postgres_pool_max_lifetime,
            timeout=settings.postgres_pool_timeout,
            check=AsyncConnectionPool.check_connection,
            reconnect_failed=_reconnect_failed,
            kwargs={
                "autocommit": True,
                "prepare_threshold": 0,
                "row_factory": dict_row,
                "keepalives": 1,
                "keepalives_idle": 30,
                "keepalives_interval": 10,
                "keepalives_count": 3,
            },
            open=False,
        )
        await pool.open(wait=True, timeout=10)
        checkpointer = AsyncPostgresSaver(pool)
        await checkpointer.setup()
    except Exception:
        if pool is not None:
            await pool.close()
        raise

    log.info(
        "Checkpointer: AsyncPostgresSaver (pool min=%s max=%s max_idle=%ss max_lifetime=%ss timeout=%ss)",
        settings.postgres_pool_min_size, settings.postgres_pool_max_size,
        settings.postgres_pool_max_idle, settings.postgres_pool_max_lifetime,
        settings.postgres_pool_timeout,
    )
    return CheckpointerHandle(checkpointer=checkpointer, pool=pool, kind="postgres")


async def init_checkpointer(settings: Settings) -> CheckpointerHandle:
    """Connect to Postgres, retrying for POSTGRES_STARTUP_RETRY_SECONDS.

    Once the window is exhausted: raises RuntimeError when Postgres is mandatory
    (prod by default), otherwise falls back to MemorySaver. Credentials are never logged.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + settings.postgres_startup_retry_seconds
    attempt = 0
    while True:
        attempt += 1
        try:
            return await _connect_postgres(settings)
        except Exception as e:  # noqa: BLE001
            remaining = deadline - loop.time()
            if remaining <= 0:
                log.error(
                    "Postgres checkpointer unavailable after %d attempt(s) (%s: %s) env=%s required=%s",
                    attempt, type(e).__name__, e, settings.igot_env, settings.postgres_is_required,
                )
                if settings.postgres_is_required:
                    raise RuntimeError(
                        "Postgres checkpointer is required but unavailable; refusing to fall back "
                        "to in-memory state. Set POSTGRES_REQUIRED=false to allow the fallback."
                    ) from e
                from langgraph.checkpoint.memory import MemorySaver

                log.warning("Falling back to MemorySaver — conversation state will NOT persist")
                return CheckpointerHandle(checkpointer=MemorySaver(), kind="memory")
            delay = min(settings.postgres_startup_retry_interval, remaining)
            log.warning(
                "Postgres checkpointer attempt %d failed (%s: %s); retrying in %.1fs (%.0fs left)",
                attempt, type(e).__name__, e, delay, remaining,
            )
            await asyncio.sleep(delay)
