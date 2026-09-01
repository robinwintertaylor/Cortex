"""SSE stream (FR-9, P1): GET /v1/stream — live events with Last-Event-ID
resume and agent/project/kind filters. Backed by Postgres LISTEN/NOTIFY (the
cortex_notify_event trigger in schema.py); a missed event is replayed from the
append-only log on reconnect."""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from fastapi import APIRouter, Depends, Header
from fastapi.responses import StreamingResponse

from .. import metrics
from ..db import get_pool
from ..events import record_to_dict
from .deps import get_conn, require_agent

router = APIRouter()

_subscribers: dict[int, asyncio.Queue] = {}
_listener_started = False


async def _listen_task() -> None:
    """One dedicated connection, dispatching pg_notify payloads to subscribers."""
    global _listener_started
    if _listener_started:
        return
    _listener_started = True
    pool = await get_pool()
    conn = await pool.acquire()
    try:
        await conn.add_listener("cortex_events", _dispatch)
        while True:
            await asyncio.sleep(3600)
    finally:
        await pool.release(conn)


def _dispatch(conn, pid, channel: str, payload: str) -> None:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return
    for q in list(_subscribers.values()):
        q.put_nowait(data)


def _matches(data: dict[str, Any], agent: str | None, project: str | None, kind: str | None) -> bool:
    if agent and data.get("agent") != agent:
        return False
    if project and data.get("project") != project:
        return False
    if kind and data.get("kind") != kind:
        return False
    return True


@router.get("/v1/stream")
async def stream(
    agent_filter: str | None = None,
    project: str | None = None,
    kind: str | None = None,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    agent=Depends(require_agent),
):
    asyncio.get_event_loop().create_task(_listen_task())

    async def gen():
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        key = id(q)
        _subscribers[key] = q
        metrics.sse_subscribers.inc()
        try:
            # replay missed events after Last-Event-ID (resume support).
            # short-lived connection — never hold a pool slot during streaming
            if last_event_id:
                try:
                    since_id = int(last_event_id.removeprefix("E-"))
                except ValueError:
                    since_id = -1
                if since_id >= 0:
                    pool = await get_pool()
                    async with pool.acquire() as conn:
                        rows = await conn.fetch(
                            "SELECT * FROM events WHERE id > $1 ORDER BY id ASC LIMIT 500",
                            since_id,
                        )
                    for r in rows:
                        d = record_to_dict(r)
                        if _matches(d, agent_filter, project, kind):
                            yield f"id: E-{d['id']}\nevent: brain\ndata: {json.dumps(d, default=str)}\n\n"
            yield f"event: ready\ndata: {{\"ok\": true}}\n\n"
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=25.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if _matches(item, agent_filter, project, kind):
                    yield f"id: E-{item['id']}\nevent: brain\ndata: {json.dumps(item, default=str)}\n\n"
        finally:
            _subscribers.pop(key, None)
            metrics.sse_subscribers.dec()

    return StreamingResponse(gen(), media_type="text/event-stream",
                            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
