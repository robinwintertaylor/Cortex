"""Cortex API assembly (PRD §8): FastAPI app on :8738.

Order matters: REST routes first, then the MCP ASGI app mounted at root (the
MCP SDK serves its streamable-HTTP endpoint at /mcp inside the mount). A pure
ASGI middleware resolves the bearer identity for /mcp requests and exposes it
to MCP tools via the current_agent contextvar."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.types import ASGIApp, Receive, Scope, Send

from ..config import get_config
from ..db import close_pool, get_pool, migrate
from ..log import get_logger, setup_logging
from ..security import current_agent, resolve_bearer
from . import routes, stream
from . import mcp as mcp_module
from .mcp import mcp

log = get_logger(__name__)


class McpAuthMiddleware:
    """Resolve MCP caller identity server-side (FR-1) for /mcp paths."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "http" and scope.get("path", "").startswith("/mcp"):
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            auth = headers.get("authorization", "")
            token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
            agent = None
            if token:
                pool = await get_pool()
                async with pool.acquire() as conn:
                    agent = await resolve_bearer(conn, token)
            if agent is None:
                await self._unauthorized(send)
                return
            current_agent.set(agent)
        await self.app(scope, receive, send)

    @staticmethod
    async def _unauthorized(send) -> None:
        body = b'{"error": "invalid or missing agent key"}'
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_config()
    setup_logging(cfg.log_level)
    await migrate()
    # warm the pool before the first request
    await get_pool()
    # SDK version tolerance: newer MCP SDKs want the session manager running
    if hasattr(mcp, "session_manager") and hasattr(mcp.session_manager, "run"):
        async with mcp.session_manager.run():
            yield
    else:
        yield
    await close_pool()


app = FastAPI(title="Cortex", version="1.0.0", lifespan=lifespan)

app.include_router(routes.router)
app.include_router(stream.router)

# MCP last: catch-all mount so /mcp reaches the SDK's streamable-HTTP server,
# wrapped in the identity middleware (bearer key → current_agent).
_mcp_asgi = mcp_module.streamable_http_app()
app.mount("/", McpAuthMiddleware(_mcp_asgi))
