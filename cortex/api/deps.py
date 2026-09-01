"""FastAPI dependencies: pool connection, bearer identity, admin, hook token."""

from __future__ import annotations

from typing import AsyncIterator

import asyncpg
from fastapi import Depends, Header, HTTPException, Request

from ..db import get_pool
from ..security import is_admin, resolve_bearer


async def get_conn() -> AsyncIterator[asyncpg.Connection]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn


async def require_agent(
    conn: asyncpg.Connection = Depends(get_conn),
    authorization: str | None = Header(default=None),
) -> asyncpg.Record:
    """Resolve identity server-side from the bearer key (FR-1 AC1/AC2)."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer key")
    agent = await resolve_bearer(conn, authorization[7:].strip())
    if agent is None:
        raise HTTPException(401, "invalid or revoked key")
    return agent


async def require_admin(
    conn: asyncpg.Connection = Depends(get_conn),
    authorization: str | None = Header(default=None),
) -> bool:
    if not is_admin(authorization):
        raise HTTPException(401, "admin key required")
    return True


async def require_hook_token(
    request: Request,
    conn: asyncpg.Connection = Depends(get_conn),
    authorization: str | None = Header(default=None),
    x_hook_token: str | None = Header(default=None),
) -> asyncpg.Record:
    """Shared-token sink auth + LAN origin check (NFR-4). Returns a synthetic
    'hook' agent row: hook writes are raw-event writes (persona P4)."""
    import hmac as _hmac

    from ..config import get_config
    from ..security import is_lan_allowed

    cfg = get_config()
    supplied = x_hook_token or (authorization[7:].strip() if authorization and
                                authorization.lower().startswith("bearer ") else None)
    if not cfg.hook_token or not supplied or not _hmac.compare_digest(supplied, cfg.hook_token):
        raise HTTPException(401, "invalid hook token")
    peer = request.client.host if request.client else ""
    if not is_lan_allowed(peer, cfg.lan_cidrs):
        raise HTTPException(403, f"hook sink not allowed from {peer}")
    return _HOOK_IDENTITY


class _HookIdentity:
    """Synthetic identity for token-authenticated hook writes (persona P4):
    raw event writes; the per-agent name travels in the hook payload."""

    def __getitem__(self, key):
        return {"id": "hook", "harness": "hooks", "role": "agent"}[key]

    def get(self, key, default=None):
        return {"id": "hook", "harness": "hooks", "role": "agent"}.get(key, default)


_HOOK_IDENTITY = _HookIdentity()
