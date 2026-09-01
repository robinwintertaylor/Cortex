"""Read-only dashboard (FR-13, P2): timeline, search box, decision browser
with supersession chains, queue/adjudication board, agent directory.

Run separately: uvicorn cortex.dashboard:app --port 8740
(compose service `dashboard`). No write operations in v1 (AC per FR-13)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .db import get_pool
from .events import record_to_dict
from .facts import fact_to_dict
from .graph import build_graph
from .notes import note_to_dict
from .queue import item_to_dict
from .search import brain_search
from .digest import digest as make_digest, render_digest_md
from .util import parse_since, payload_dict

_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app = FastAPI(title="Cortex Dashboard", docs_url=None, redoc_url=None)
# vendored vis-network (local-first — no CDN call from the browser)
app.mount("/static", StaticFiles(
    directory=str(Path(__file__).parent / "templates" / "static")), name="static")


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


@app.get("/documents", response_class=HTMLResponse)
async def documents_page(request: Request, project: str | None = None):
    """Uploaded files (brain_upload_file), shared across every harness."""
    pool = await _conn()
    async with pool.acquire() as conn:
        where = "WHERE note_type = 'document'"
        args: list[Any] = []
        if project:
            args.append(project)
            where += f" AND project = ${len(args)}"
        rows = await conn.fetch(
            f"SELECT * FROM notes {where} ORDER BY ts DESC LIMIT 200", *args)
    return _templates.TemplateResponse(request, "documents.html", {
        "docs": [note_to_dict(r) for r in rows], "project": project or "",
    })


@app.get("/documents/{note_id}/download")
async def documents_download(note_id: str):
    """Serves the raw bytes directly (no bearer key): the dashboard is
    already unauthenticated read-only (FR-13), and a plain browser link
    can't attach an Authorization header the way /v1/brain_file/{id} needs."""
    from . import files as blobstore

    pool = await _conn()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM notes WHERE id = $1 AND note_type = 'document'", note_id)
    if row is None or not row["storage_path"]:
        return Response(status_code=404)
    content = blobstore.read_blob(row["storage_path"])
    filename = row["original_filename"] or "download"
    return Response(
        content=content, media_type=row["mime_type"] or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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


@app.get("/graph", response_class=HTMLResponse)
async def graph_page(request: Request, project: str | None = None,
                     history: bool = False, limit: int = 300):
    """Knowledge-graph view: entities as nodes, facts as edges (FR-13).
    Subjects, tools/MCPs/apps in use, decisions, research links — governed
    by the same fact model as everything else."""
    pool = await _conn()
    async with pool.acquire() as conn:
        entities = await conn.fetch(
            """
            SELECT en.id, en.name, en.etype, en.summary,
                   count(f.id) AS fact_count
            FROM entities en
            LEFT JOIN facts f ON f.subj = en.id AND f.valid_to IS NULL
            GROUP BY en.id
            ORDER BY fact_count DESC, en.name
            LIMIT $1
            """,
            min(limit, 1000),
        )
        args: list[Any] = [min(limit, 1000) * 4]
        proj_where = ""
        if project:
            args.append(project)
            proj_where = f" AND (e.project = ${len(args)} OR e.project IS NULL)"
        facts_rows = await conn.fetch(
            f"""
            SELECT f.*, e.agent AS agent, e.harness AS harness
            FROM facts f
            LEFT JOIN events e ON e.id = f.episode_id
            WHERE TRUE{proj_where}
            ORDER BY f.valid_from DESC
            LIMIT $1
            """,
            *args,
        )
        args_docs: list[Any] = [min(limit, 1000)]
        docs_proj_where = ""
        if project:
            args_docs.append(project)
            docs_proj_where = f" AND (project = ${len(args_docs)} OR project IS NULL)"
        docs_rows = await conn.fetch(
            f"""
            SELECT id, title, note_type, project, tags, author, source_url,
                   original_filename, links
            FROM notes
            WHERE TRUE{docs_proj_where}
            ORDER BY ts DESC
            LIMIT $1
            """,
            *args_docs,
        )
        doc_link_rows = await conn.fetch(
            "SELECT note_id, entity_id, score, method FROM doc_links WHERE note_id = ANY($1::uuid[])",
            [r["id"] for r in docs_rows],
        ) if docs_rows else []
        g = build_graph(entities, facts_rows, docs=docs_rows, doc_links=doc_link_rows,
                        include_history=history, max_nodes=limit)
        g["stats"]["docs"] = sum(1 for n in g["nodes"] if n["group"] == "doc")
    return _templates.TemplateResponse(request, "graph.html", {
        "graph": g, "project": project or "", "history": history,
        "limit": limit,
    })


@app.get("/v1/brain_graph_node")
async def graph_node(id: str):
    """Facts for one graph node (by entity name) — side-panel detail view."""
    pool = await _conn()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT f.*, e.agent AS agent, e.harness AS harness
            FROM facts f
            LEFT JOIN events e ON e.id = f.episode_id
            WHERE lower(f.subj_name) = lower($1)
            ORDER BY (f.valid_to IS NULL) DESC, f.valid_from DESC LIMIT 100
            """,
            id,
        )
    return {"facts": [
        {**fact_to_dict(r), "harness": r["harness"] or r["agent"] or "unknown",
         "agent": r["agent"]}
        for r in rows
    ]}
