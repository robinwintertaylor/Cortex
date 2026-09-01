"""Read-only dashboard (FR-13, P2): timeline, search box, decision browser
with supersession chains, queue/adjudication board, agent directory.

Run separately: uvicorn cortex.dashboard:app --port 8740
(compose service `dashboard`). No write operations in v1 (AC per FR-13)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .db import get_pool
from .events import record_to_dict
from .facts import fact_to_dict
from .queue import item_to_dict
from .search import brain_search
from .digest import digest as make_digest, render_digest_md
from .util import parse_since, payload_dict

_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app = FastAPI(title="Cortex Dashboard", docs_url=None, redoc_url=None)


async def _conn():
    pool = await get_pool()
    return pool


@app.get("/", response_class=HTMLResponse)
async def timeline(request: Request, agent: str | None = None, kind: str | None = None,
                   project: str | None = None, since: str | None = "48h"):
    pool = await _conn()
    try:
        since_dt = parse_since(since)
    except ValueError:
        since_dt = parse_since("48h")
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM events
            WHERE ($1::text IS NULL OR agent = $1)
              AND ($2::text IS NULL OR kind = $2)
              AND ($3::text IS NULL OR project = $3)
              AND ts >= $4::timestamptz
            ORDER BY ts DESC LIMIT 200
            """,
            agent, kind, project, since_dt,
        )
        agents = [r["id"] for r in await conn.fetch("SELECT id FROM agents ORDER BY id")]
    events = [record_to_dict(r) for r in rows]
    return _templates.TemplateResponse(request, "timeline.html", {
        "events": events, "agents": agents, "filters": {
            "agent": agent or "", "kind": kind or "", "project": project or "",
            "since": since or "48h"},
    })


@app.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str = "", mode: str = "hybrid", limit: int = 20):
    results: list[dict[str, Any]] = []
    pool = await _conn()
    if q:
        async with pool.acquire() as conn:
            results = await brain_search(conn, q, mode=mode, limit=limit)
    return _templates.TemplateResponse(request, "search.html", {
        "q": q, "mode": mode, "results": results,
    })


@app.get("/decisions", response_class=HTMLResponse)
async def decisions(request: Request):
    pool = await _conn()
    async with pool.acquire() as conn:
        evs = await conn.fetch(
            "SELECT * FROM events WHERE kind = 'decision' ORDER BY id DESC LIMIT 100")
        out = []
        for ev in evs:
            facts_rows = await conn.fetch(
                "SELECT * FROM facts WHERE episode_id = $1 ORDER BY valid_from DESC",
                ev["id"])
            fact = facts_rows[0] if facts_rows else None
            chain: list[dict] = []
            if fact:
                # walk the supersession chain backwards from this fact
                current = fact
                seen = 0
                while current and seen < 10:
                    chain.append(fact_to_dict(current))
                    if current["superseded_by"]:
                        current = await conn.fetchrow(
                            "SELECT * FROM facts WHERE id = $1", current["superseded_by"])
                    else:
                        break
                    seen += 1
            p = payload_dict(ev["payload"])
            out.append({
                "adr": p.get("adr") or f"D-{ev['id']}", "title": p.get("title", ""),
                "choice": p.get("choice", ""), "agent": ev["agent"],
                "project": ev["project"], "ts": ev["ts"],
                "status": "superseded" if (fact and fact["valid_to"]) else "active",
                "chain": list(reversed(chain)),
            })
    return _templates.TemplateResponse(request, "decisions.html", {"decisions": out})


@app.get("/queue", response_class=HTMLResponse)
async def queue_board(request: Request):
    pool = await _conn()
    async with pool.acquire() as conn:
        items = await conn.fetch(
            "SELECT * FROM queue WHERE status IN ('open','claimed') ORDER BY created DESC LIMIT 200")
        digest_md = render_digest_md(await make_digest(conn, "7d"))
    return _templates.TemplateResponse(request, "queue.html", {
        "items": [item_to_dict(i) for i in items], "digest_md": digest_md,
    })


@app.get("/agents", response_class=HTMLResponse)
async def agents_page(request: Request):
    pool = await _conn()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT a.*, (SELECT count(*) FROM events e WHERE e.agent = a.id) AS event_count
            FROM agents a ORDER BY event_count DESC
            """)
    return _templates.TemplateResponse(request, "agents.html", {"agents": [dict(r) for r in rows]})
