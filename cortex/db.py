"""asyncpg pool + schema migration. All queries in Cortex go through this pool."""

from __future__ import annotations

import asyncpg
from pgvector.asyncpg import register_vector

from .config import get_config
from .log import get_logger
from .schema import ddl_statements

log = get_logger(__name__)
_pool: asyncpg.Pool | None = None


async def _init_conn(conn: asyncpg.Connection) -> None:
    await register_vector(conn)


async def _ensure_extensions(dsn: str) -> None:
    """Install vector/pgcrypto before the pool init callback.

    pgvector's register_vector looks up the `vector` type OID. On a fresh
    database that type does not exist until CREATE EXTENSION runs, so pool
    creation would otherwise fail and migrate would never execute.
    """
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    finally:
        await conn.close()


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        cfg = get_config()
        await _ensure_extensions(cfg.database_url)
        _pool = await asyncpg.create_pool(
            cfg.database_url,
            min_size=1,
            max_size=10,
            init=_init_conn,
        )
        log.info("db pool ready")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def migrate() -> None:
    """Apply the schema. Idempotent; dim comes from config (must match first boot)."""
    cfg = get_config()
    pool = await get_pool()
    async with pool.acquire() as conn:
        for stmt in ddl_statements(cfg.embed_dim):
            await conn.execute(stmt)
    log.info("schema migrated", extra={"err": ""})


async def fetch(query: str, *args) -> list[asyncpg.Record]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(query, *args)


async def fetchrow(query: str, *args) -> asyncpg.Record | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(query, *args)


async def execute(query: str, *args) -> str:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.execute(query, *args)
