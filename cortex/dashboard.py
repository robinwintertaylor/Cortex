"""Read-only dashboard (FR-13, P2).

Organised around the questions an owner actually asks rather than around the
tables underneath:

  /           Briefing    what changed, and what is waiting
  /map        Map         where everything sits, semantically
  /knowledge  Knowledge   entities, facts and lessons
  /entity/X   Entity      everything known about one thing — the tracing hub
  /decisions  Decisions   what was decided, and whether it still holds
  /documents  Documents   the artifacts themselves
  /activity   Activity    the raw event log
  /agents     Agents      who is writing, and what tools they have

No write operations (AC per FR-13).
Run separately: uvicorn cortex.dashboard:app --port 8740
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import mapproj
from .db import get_pool
from .digest import digest as make_digest
from .events import KINDS, record_to_dict
from .facts import fact_to_dict
from .graph import build_graph
from .notes import note_to_dict
from .projects import aliases_of, canonical
from .queue import item_to_dict
from .search import brain_search
from .util import parse_since, payload_dict

_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app = FastAPI(title="Cortex Dashboard", docs_url=None, redoc_url=None)
# vendored assets (stylesheet, map renderer, vis-network) — local-first, no CDN
app.mount("/static", StaticFiles(
    directory=str(Path(__file__).parent / "templates" / "static")), name="static")


async def _conn():
    return await get_pool()


async def _known_projects(conn) -> list[str]:
    """Canonical project names, for filter dropdowns."""
    rows = await conn.fetch(
        "SELECT DISTINCT project FROM events WHERE project IS NOT NULL AND project <> ''")
    return sorted({canonical(r["project"]) for r in rows if canonical(r["project"])})


def _project_filter(args: list[Any], project: str | None, column: str) -> str:
    """Append an alias-expanded project predicate. Historical rows keep the
    spelling they were written with, so an equality test would miss them."""
    if not project:
        return ""
    args.append(aliases_of(project))
    return f" AND {column} = ANY(${len(args)})"


# ── briefing ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def briefing(request: Request, since: str | None = "7d"):
    """What changed and what is waiting — the structured form of the digest.

    The digest has always returned a rich dict; the old page rendered it to
    markdown and dumped it into a <pre>, which threw away every link."""
    pool = await _conn()
    async with pool.acquire() as conn:
        d = await make_digest(conn, since)
        counts = await conn.fetchrow(
            """
            SELECT (SELECT count(*) FROM events)                       AS events,
                   (SELECT count(*) FROM entities)                     AS entities,
                   (SELECT count(*) FROM facts WHERE valid_to IS NULL) AS facts,
                   (SELECT count(*) FROM facts
                     WHERE valid_to IS NULL AND obj IS NOT NULL)       AS linked_facts,
                   (SELECT count(*) FROM lessons)                      AS lessons,
                   (SELECT count(*) FROM notes
                     WHERE note_type = 'document')                     AS documents,
                   (SELECT count(*) FROM queue WHERE status = 'open')  AS queue_open
            """)
        queue_rows = await conn.fetch(
            "SELECT * FROM queue WHERE status IN ('open','claimed') "
            "ORDER BY created DESC LIMIT 50")
    return _templates.TemplateResponse(request, "briefing.html", {
        "d": d, "counts": dict(counts), "since": since or "7d",
        "queue": [item_to_dict(i) for i in queue_rows], "active": "briefing",
    })


# ── activity (the raw log) ───────────────────────────────────────────────────

@app.get("/activity", response_class=HTMLResponse)
async def activity(request: Request, agent: str | None = None, kind: str | None = None,
                   project: str | None = None, since: str | None = "48h"):
    pool = await _conn()
    try:
        since_dt = parse_since(since)
    except ValueError:
        since_dt = parse_since("48h")
    async with pool.acquire() as conn:
        args: list[Any] = [since_dt]
        where = "ts >= $1"
        if agent:
            args.append(agent)
            where += f" AND agent = ${len(args)}"
        if kind:
            args.append(kind)
            where += f" AND kind = ${len(args)}"
        where += _project_filter(args, project, "project")
        rows = await conn.fetch(
            f"SELECT * FROM events WHERE {where} ORDER BY ts DESC LIMIT 200", *args)
        agents = [r["id"] for r in await conn.fetch("SELECT id FROM agents ORDER BY id")]
        projects = await _known_projects(conn)
    return _templates.TemplateResponse(request, "activity.html", {
        "events": [record_to_dict(r) for r in rows],
        "agents": agents, "projects": projects, "kinds": list(KINDS),
        "filters": {"agent": agent or "", "kind": kind or "",
                    "project": project or "", "since": since or "48h"},
        "active": "activity",
    })


# ── search ───────────────────────────────────────────────────────────────────

@app.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str = "", mode: str = "hybrid",
                 type: str | None = None, agent: str | None = None,
                 project: str | None = None, limit: int = 30):
    """brain_search has always accepted type/agent/project facets; the page
    never exposed them."""
    results: list[dict[str, Any]] = []
    pool = await _conn()
    async with pool.acquire() as conn:
        if q:
            results = await brain_search(conn, q, mode=mode, limit=limit,
                                         type=type or None, agent=agent or None,
                                         project=project or None)
        agents = [r["id"] for r in await conn.fetch("SELECT id FROM agents ORDER BY id")]
        projects = await _known_projects(conn)
    return _templates.TemplateResponse(request, "search.html", {
        "q": q, "mode": mode, "results": results, "agents": agents,
        "projects": projects,
        "filters": {"type": type or "", "agent": agent or "", "project": project or ""},
        "active": "search",
    })


# ── knowledge ────────────────────────────────────────────────────────────────

@app.get("/knowledge", response_class=HTMLResponse)
async def knowledge(request: Request, tab: str = "entities", q: str | None = None):
    """Entities, facts and lessons in one place. The 104 lessons in particular
    had no page at all — they existed only inside the digest's markdown."""
    tab = tab if tab in ("entities", "facts", "lessons") else "entities"
    pool = await _conn()
    async with pool.acquire() as conn:
        entities = facts = lessons = []
        if tab == "entities":
            args: list[Any] = []
            where = ""
            if q:
                args.append(f"%{q}%")
                where = f" WHERE e.name ILIKE ${len(args)}"
            entities = await conn.fetch(
                f"""
                SELECT e.id, e.name, e.etype, e.summary,
                       (SELECT count(*) FROM facts f
                         WHERE f.subj = e.id AND f.valid_to IS NULL) AS fact_count,
                       (SELECT count(*) FROM facts f
                         WHERE f.obj = e.id AND f.valid_to IS NULL)  AS inbound_count,
                       (SELECT count(*) FROM doc_links dl
                         WHERE dl.entity_id = e.id)                  AS doc_count
                FROM entities e{where}
                ORDER BY fact_count DESC, e.name LIMIT 400
                """, *args)
        elif tab == "facts":
            args = []
            where = "WHERE f.valid_to IS NULL"
            if q:
                args.append(f"%{q}%")
                where += f" AND (f.subj_name ILIKE ${len(args)} OR f.obj_text ILIKE ${len(args)})"
            facts = await conn.fetch(
                f"""
                SELECT f.*, e.agent, e.harness, e.project
                FROM facts f LEFT JOIN events e ON e.id = f.episode_id
                {where} ORDER BY f.valid_from DESC LIMIT 300
                """, *args)
        else:
            args = []
            where = ""
            if q:
                args.append(f"%{q}%")
                where = f" WHERE statement ILIKE ${len(args)}"
            lessons = await conn.fetch(
                f"SELECT * FROM lessons{where} ORDER BY ts DESC LIMIT 300", *args)
        totals = await conn.fetchrow(
            """
            SELECT (SELECT count(*) FROM entities)                     AS entities,
                   (SELECT count(*) FROM facts WHERE valid_to IS NULL) AS facts,
                   (SELECT count(*) FROM lessons)                      AS lessons
            """)
    return _templates.TemplateResponse(request, "knowledge.html", {
        "tab": tab, "q": q or "", "totals": dict(totals),
        "entities": [dict(r) for r in entities],
        "facts": [{**fact_to_dict(r), "agent": r["agent"],
                   "project": canonical(r["project"])} for r in facts],
        "lessons": [{"id": str(r["id"]), "statement": r["statement"],
                     "project": canonical(r["project"]), "status": r["status"],
                     "confidence": r["confidence"],
                     "verified_by": r["verified_by"],
                     "ts": r["ts"].isoformat()} for r in lessons],
        "active": "knowledge",
    })


@app.get("/entity/{name}", response_class=HTMLResponse)
async def entity_page(request: Request, name: str):
    """Everything known about one thing.

    This is the page that makes the graph traceable for a human: outbound facts,
    *inbound* facts (only possible now that facts.obj resolves to entities),
    linked documents, lessons that mention it, who contributed, and the episodes
    that produced it."""
    pool = await _conn()
    async with pool.acquire() as conn:
        ent = await conn.fetchrow(
            "SELECT * FROM entities WHERE lower(name) = lower($1)", name)
        outbound = await conn.fetch(
            """
            SELECT f.*, e.agent, e.harness, e.project,
                   o.name AS obj_name, o.etype AS obj_etype
            FROM facts f
            LEFT JOIN events e ON e.id = f.episode_id
            LEFT JOIN entities o ON o.id = f.obj
            WHERE lower(f.subj_name) = lower($1)
            ORDER BY (f.valid_to IS NULL) DESC, f.valid_from DESC LIMIT 200
            """, name)
        inbound = await conn.fetch(
            """
            SELECT f.*, e.agent, e.harness, e.project
            FROM facts f
            LEFT JOIN events e ON e.id = f.episode_id
            JOIN entities o ON o.id = f.obj
            WHERE lower(o.name) = lower($1) AND f.valid_to IS NULL
            ORDER BY f.valid_from DESC LIMIT 100
            """, name) if ent else []
        docs = await conn.fetch(
            """
            SELECT n.id, n.title, n.original_filename, n.note_type, n.project,
                   n.tags, dl.score, dl.method
            FROM doc_links dl JOIN notes n ON n.id = dl.note_id
            WHERE dl.entity_id = $1 ORDER BY dl.score DESC LIMIT 60
            """, ent["id"]) if ent else []
        lessons = await conn.fetch(
            "SELECT * FROM lessons WHERE statement ILIKE $1 ORDER BY ts DESC LIMIT 25",
            f"%{name}%")
        # named columns, not e.* — DISTINCT over the whole row would have to
        # compare the 1024-d embedding and the generated tsvector too
        episodes = await conn.fetch(
            """
            SELECT DISTINCT e.id, e.ts, e.agent, e.harness, e.session,
                            e.kind, e.project, e.payload
            FROM events e
            JOIN facts f ON f.episode_id = e.id
            WHERE lower(f.subj_name) = lower($1)
            ORDER BY e.ts DESC LIMIT 40
            """, name)
        neighbours = []
        if ent and ent["map_cluster"] is not None and ent["map_cluster"] >= 0:
            neighbours = await conn.fetch(
                """
                SELECT name, etype FROM entities
                WHERE map_cluster = $1 AND id <> $2 ORDER BY name LIMIT 20
                """, ent["map_cluster"], ent["id"])
        region = await conn.fetchrow(
            "SELECT name FROM map_clusters WHERE id = $1",
            ent["map_cluster"]) if ent and ent["map_cluster"] is not None else None

    contributors: dict[str, int] = {}
    for r in list(outbound) + list(inbound):
        who = r["harness"] or r["agent"] or "unknown"
        contributors[who] = contributors.get(who, 0) + 1

    return _templates.TemplateResponse(request, "entity.html", {
        "name": ent["name"] if ent else name,
        "entity": dict(ent) if ent else None,
        "region": region["name"] if region else None,
        "outbound": [{**fact_to_dict(r), "agent": r["agent"],
                      "harness": r["harness"] or r["agent"],
                      "project": canonical(r["project"]),
                      "obj_name": r["obj_name"], "obj_etype": r["obj_etype"]}
                     for r in outbound],
        "inbound": [{**fact_to_dict(r), "agent": r["agent"],
                     "harness": r["harness"] or r["agent"],
                     "project": canonical(r["project"])} for r in inbound],
        "docs": [{"id": str(r["id"]),
                  "label": r["original_filename"] or r["title"],
                  "note_type": r["note_type"], "project": canonical(r["project"]),
                  "tags": list(r["tags"] or []),
                  "score": round(float(r["score"]), 2), "method": r["method"]}
                 for r in docs],
        "lessons": [{"id": str(r["id"]), "statement": r["statement"]} for r in lessons],
        "episodes": [record_to_dict(r) for r in episodes],
        "neighbours": [{"name": r["name"], "etype": r["etype"]} for r in neighbours],
        "contributors": sorted(contributors.items(), key=lambda kv: -kv[1]),
        "active": "knowledge",
    })


# ── decisions ────────────────────────────────────────────────────────────────

@app.get("/decisions", response_class=HTMLResponse)
async def decisions(request: Request, project: str | None = None):
    pool = await _conn()
    async with pool.acquire() as conn:
        args: list[Any] = []
        where = "WHERE kind = 'decision'"
        where += _project_filter(args, project, "project")
        evs = await conn.fetch(
            f"SELECT * FROM events {where} ORDER BY id DESC LIMIT 100", *args)
        out = []
        for ev in evs:
            facts_rows = await conn.fetch(
                "SELECT * FROM facts WHERE episode_id = $1 ORDER BY valid_from DESC",
                ev["id"])
            fact = facts_rows[0] if facts_rows else None
            chain: list[dict] = []
            head = fact
            if fact:
                current, seen = fact, 0
                while current and seen < 10:
                    chain.append(fact_to_dict(current))
                    head = current
                    if not current["superseded_by"]:
                        break
                    current = await conn.fetchrow(
                        "SELECT * FROM facts WHERE id = $1", current["superseded_by"])
                    seen += 1
            p = payload_dict(ev["payload"])
            out.append({
                "adr": p.get("adr") or f"D-{ev['id']}", "title": p.get("title", ""),
                "choice": p.get("choice", ""), "agent": ev["agent"],
                "project": canonical(ev["project"]), "ts": ev["ts"].isoformat(),
                # the chain HEAD decides whether this decision still stands.
                # Reading it off the oldest fact marked live decisions as
                # superseded the moment they had ever been revised.
                "status": "superseded" if (head and head["valid_to"]) else "active",
                "chain": chain,
            })
        projects = await _known_projects(conn)
    return _templates.TemplateResponse(request, "decisions.html", {
        "decisions": out, "projects": projects, "project": project or "",
        "active": "decisions",
    })


# ── queue ────────────────────────────────────────────────────────────────────

@app.get("/queue", response_class=HTMLResponse)
async def queue_board(request: Request):
    pool = await _conn()
    async with pool.acquire() as conn:
        items = await conn.fetch(
            "SELECT * FROM queue WHERE status IN ('open','claimed') "
            "ORDER BY created DESC LIMIT 200")
    return _templates.TemplateResponse(request, "queue.html", {
        "items": [item_to_dict(i) for i in items], "active": "briefing",
    })


# ── documents ────────────────────────────────────────────────────────────────

@app.get("/documents", response_class=HTMLResponse)
async def documents_page(request: Request, project: str | None = None,
                         q: str | None = None):
    """Uploaded files (brain_upload_file), shared across every harness."""
    pool = await _conn()
    async with pool.acquire() as conn:
        args: list[Any] = []
        where = "WHERE note_type = 'document'"
        where += _project_filter(args, project, "project")
        if q:
            args.append(f"%{q}%")
            where += (f" AND (original_filename ILIKE ${len(args)} "
                      f"OR title ILIKE ${len(args)})")
        rows = await conn.fetch(
            f"SELECT * FROM notes {where} ORDER BY ts DESC LIMIT 200", *args)
        projects = await _known_projects(conn)
    return _templates.TemplateResponse(request, "documents.html", {
        "docs": [note_to_dict(r) for r in rows], "project": project or "",
        "q": q or "", "projects": projects, "active": "documents",
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


# ── agents & tools ───────────────────────────────────────────────────────────

# key_hash must never reach a template context — the page passed whole rows
# through dict(r) before, which carried the argon2 hash into the render.
_AGENT_FIELDS = ("id", "name", "harness", "role", "revoked", "last_seen",
                 "created", "event_count")


@app.get("/agents", response_class=HTMLResponse)
async def agents_page(request: Request):
    pool = await _conn()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT a.*, (SELECT count(*) FROM events e WHERE e.agent = a.id) AS event_count
            FROM agents a ORDER BY event_count DESC
            """)
        tools = await conn.fetch(
            """
            SELECT t.*, e.name AS entity_name FROM tools t
            LEFT JOIN entities e ON e.id = t.entity_id
            ORDER BY t.agent, t.tool_kind, lower(t.name)
            """)
    agents = []
    for r in rows:
        d = {k: r[k] for k in _AGENT_FIELDS}
        for key in ("last_seen", "created"):
            d[key] = d[key].isoformat() if d[key] else None
        agents.append(d)
    by_agent: dict[str, list[dict]] = {}
    for t in tools:
        by_agent.setdefault(t["agent"], []).append({
            "name": t["name"], "kind": t["tool_kind"], "server": t["server"],
            "version": t["version"], "entity_name": t["entity_name"],
            "last_declared": t["last_declared"].isoformat() if t["last_declared"] else None,
        })
    return _templates.TemplateResponse(request, "agents.html", {
        "agents": agents, "tools_by_agent": by_agent,
        "tool_count": len(tools), "active": "agents",
    })


# ── map + graph ──────────────────────────────────────────────────────────────

@app.get("/map", response_class=HTMLResponse)
async def map_page(request: Request, project: str | None = None):
    """Semantic map: every entity and document placed by embedding similarity.

    The force-directed /graph view re-ran its physics on every load, so nothing
    stayed where you left it. Here position is derived from the embeddings
    (cortex/mapproj.py) and cached, so the layout is the same every time and
    proximity actually means something."""
    pool = await _conn()
    async with pool.acquire() as conn:
        doc = await mapproj.load_map(conn, project=project)
    return _templates.TemplateResponse(request, "map.html", {
        "map": doc, "project": project or "", "active": "map",
    })


@app.get("/graph", response_class=HTMLResponse)
async def graph_page(request: Request, project: str | None = None,
                     history: bool = False, limit: int = 300):
    """Knowledge-graph view: entities as nodes, facts as edges (FR-13).
    The local, relationship-shaped counterpart to /map's global overview."""
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
            args.append(aliases_of(project))
            proj_where = f" AND (e.project = ANY(${len(args)}) OR e.project IS NULL)"
        facts_rows = await conn.fetch(
            f"""
            SELECT f.*, e.agent AS agent, e.harness AS harness, e.project AS project
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
            args_docs.append(aliases_of(project))
            docs_proj_where = f" AND (project = ANY(${len(args_docs)}) OR project IS NULL)"
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
        "limit": limit, "active": "map",
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
