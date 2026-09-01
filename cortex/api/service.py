"""Service operations — the single implementation behind REST /v1/*, the MCP
tools, and the CLI. Every op takes an asyncpg connection + the RESOLVED agent
row (identity from the bearer key, FR-1)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import asyncpg

from .. import capture, events, facts, notes, queue
from ..digest import context as make_context
from ..digest import digest as make_digest
from ..digest import render_digest_md
from ..search import timed_search
from ..util import dump_pg_json, trunc


class PermissionDenied(Exception):
    pass


# ── writes ────────────────────────────────────────────────────────────────

async def log_action(conn, agent: asyncpg.Record, *, summary: str, outcome: str | None = None,
                     files: list[str] | None = None, session: str | None = None,
                     project: str | None = None, harness: str | None = None,
                     idempotency_key: str | None = None) -> dict:
    row = await events.append(
        conn,
        agent=agent["id"], kind="action",
        harness=harness or agent["harness"], session=session, project=project,
        idempotency_key=idempotency_key,
        payload={"summary": trunc(summary, 2000), "outcome": outcome,
                 "files": files or []},
    )
    return {"event_id": row["id"], "ts": row["ts"].isoformat(), "status": "appended"}


async def log_decision(conn, agent: asyncpg.Record, *, title: str, options: list[str],
                       choice: str, rationale: str | None = None,
                       project: str | None = None, session: str | None = None,
                       idempotency_key: str | None = None) -> dict:
    """ADR event + fact (FR-7). ADR id = D-<event_id>; decision facts are
    authoritative (pred='decided') and created in the same transaction."""
    async with conn.transaction():
        ev = await events.append(
            conn,
            agent=agent["id"], kind="decision",
            harness=agent["harness"], session=session, project=project,
            idempotency_key=idempotency_key,
            payload={"title": trunc(title, 500), "options": options,
                     "choice": choice, "rationale": trunc(rationale or "", 2000),
                     "adr": None},
        )
        adr = f"D-{ev['id']}"
        await conn.execute("UPDATE events SET payload = payload || $1::jsonb WHERE id = $2",
                           dump_pg_json({"adr": adr}), ev["id"])
        fact = await facts.add_fact(
            conn, subj_name=project or title, pred="decided", obj_text=choice,
            episode_id=ev["id"], kind="decided", confidence=0.95, rationale=rationale,
        )
    return {"event_id": ev["id"], "fact_id": str(fact["id"]), "adr": adr, "status": "active"}


async def lesson(conn, agent: asyncpg.Record, *, statement: str, verified_by: str | None = None,
                 project: str | None = None, session: str | None = None,
                 idempotency_key: str | None = None) -> dict:
    async with conn.transaction():
        ev = await events.append(
            conn,
            agent=agent["id"], kind="lesson",
            harness=agent["harness"], session=session, project=project,
            idempotency_key=idempotency_key,
            payload={"statement": trunc(statement, 2000), "verified_by": verified_by},
        )
        lesson_row = await notes.add_lesson(
            conn, statement=statement, verified_by=verified_by,
            project=project, event_id=ev["id"], confidence=0.9,
        )
    return {"event_id": ev["id"], "lesson_id": str(lesson_row["id"]), "status": "active"}


async def note(conn, agent: asyncpg.Record, *, title: str, body: str,
               type: str = "note", tags: list[str] | None = None,
               project: str | None = None, session: str | None = None) -> dict:
    async with conn.transaction():
        ev = await events.append(
            conn,
            agent=agent["id"], kind="note",
            harness=agent["harness"], session=session, project=project,
            payload={"title": trunc(title, 500), "note": trunc(body, 200_000),
                     "tags": tags or []},
        )
        n = await notes.create_note(
            conn, title=title, body=body, author=agent["id"], tags=tags,
            project=project, note_type=type, event_id=ev["id"],
        )
    return {"event_id": ev["id"], "note_id": str(n["id"]), "status": "active"}


async def capture_url(conn, agent: asyncpg.Record, *, url: str, note_text: str | None = None,
                      session: str | None = None, project: str | None = None) -> dict:
    return await capture.capture_url(
        conn, url=url, agent=agent["id"], harness=agent["harness"],
        note=note_text, session=session, project=project,
    )


class UploadTooLarge(ValueError):
    pass


async def upload_file(conn, agent: asyncpg.Record, *, filename: str, content_base64: str,
                      project: str | None = None, tags: list[str] | None = None,
                      session: str | None = None) -> dict:
    """Store a file (any harness, base64 over MCP/REST) as a searchable,
    downloadable note. Text is extracted best-effort and embedded by the
    librarian exactly like any other note (FR-13-adjacent: shared uploads)."""
    import base64

    from .. import files as blobstore
    from ..config import get_config

    content = base64.b64decode(content_base64)
    cfg = get_config()
    if len(content) > cfg.upload_max_bytes:
        raise UploadTooLarge(f"{len(content)} bytes exceeds {cfg.upload_max_bytes} cap")

    mime_type = _guess_mime(filename)
    async with conn.transaction():
        sha256, storage_path, size = blobstore.save_blob(content)
        text = blobstore.extract_text(filename, mime_type, content)
        body = text[:20000] if text else "(no extractable text for this file type)"
        ev = await events.append(
            conn,
            agent=agent["id"], kind="upload",
            harness=agent["harness"], session=session, project=project,
            payload={"filename": filename, "size_bytes": size, "mime_type": mime_type,
                     "sha256": sha256, "tags": tags or []},
        )
        n = await notes.create_note(
            conn, title=f"File: {filename}", body=body, author=agent["id"],
            tags=tags, project=project, note_type="document", event_id=ev["id"],
            mime_type=mime_type, size_bytes=size, sha256=sha256,
            storage_path=storage_path, original_filename=filename,
        )
    return {"event_id": ev["id"], "note_id": str(n["id"]), "filename": filename,
            "size_bytes": size, "mime_type": mime_type, "sha256": sha256,
            "text_extracted": bool(text)}


def _guess_mime(filename: str) -> str:
    import mimetypes

    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


async def sweep(conn, agent: asyncpg.Record, urls: list[str],
                project: str | None = None) -> list[dict]:
    """Nightly research sweep for cron (FR-11)."""
    out = []
    for url in urls[:20]:
        try:
            out.append(await capture_url(conn, agent=agent, url=url, project=project))
        except Exception as e:
            out.append({"url": url, "error": str(e)})
    return out


# ── queue ──────────────────────────────────────────────────────────────────

async def queue_add(conn, agent: asyncpg.Record, *, title: str, project: str | None = None,
                    detail: dict | None = None) -> dict:
    async with conn.transaction():
        item = await queue.add(conn, title=title, created_by=agent["id"],
                               project=project, detail=detail)
        await events.append(conn, agent=agent["id"], kind="note",
                            harness=agent["harness"], project=project,
                            payload={"summary": f"queue_add: {title}",
                                     "item_id": str(item["id"])})
    return queue.item_to_dict(item)


async def queue_claim(conn, agent: asyncpg.Record, *, item_id: str) -> dict:
    async with conn.transaction():
        item = await queue.claim(conn, item_id=item_id, agent=agent["id"])
        if item is None:
            cur = await conn.fetchrow("SELECT claimed_by FROM queue WHERE id = $1",
                                      _uuid(item_id))
            who = cur["claimed_by"] if cur else "unknown"
            return {"ok": False, "error": f"already claimed by {who}", "item_id": item_id}
        await queue.log_queue_event(conn, agent=agent["id"], kind="queue_claim", item=item,
                                    harness=agent["harness"])
    d = queue.item_to_dict(item)
    d["ok"] = True
    return d


async def queue_complete(conn, agent: asyncpg.Record, *, item_id: str,
                         outcome: str = "done") -> dict:
    async with conn.transaction():
        item = await queue.complete(conn, item_id=item_id, agent=agent["id"], outcome=outcome)
        if item is None:
            return {"ok": False, "error": "not yours to complete (unclaimed or other agent)",
                    "item_id": item_id}
        await queue.log_queue_event(conn, agent=agent["id"], kind="queue_complete", item=item,
                                    harness=agent["harness"])
    d = queue.item_to_dict(item)
    d["ok"] = True
    return d


# ── owner-authoritative supersession (FR-7) ────────────────────────────────

async def supersede_fact(conn, agent: asyncpg.Record, *, fact_id: str, new_value: str,
                         rationale: str | None = None) -> dict:
    if (agent.get("role") or "agent") not in ("owner", "admin"):
        raise PermissionDenied("only the owner (or admin) may supersede facts directly")
    async with conn.transaction():
        new = await facts.supersede(conn, fact_id, new_value=new_value,
                                    rationale=rationale, kind="owner", confidence=0.99)
        await conn.execute(
            "UPDATE queue SET status = 'adjudicated' WHERE fact_id = $1 AND status = 'open'",
            _uuid(fact_id),
        )
    return {"old_fact_id": fact_id, "new_fact_id": str(new["id"]), "status": "superseded"}


# ── reads ───────────────────────────────────────────────────────────────────

async def search(conn, agent, *, query: str, mode: str = "hybrid", type: str | None = None,
                 project: str | None = None, tag: str | None = None,
                 since: str | None = None, limit: int = 10) -> dict:
    results = await timed_search(conn, query, mode=mode, type=type, project=project,
                                  tag=tag, since=since, limit=limit)
    return {"results": results, "count": len(results)}


async def recent(conn, agent, *, agent_filter: str | None = None, project: str | None = None,
                 kind: str | None = None, since: str | None = None, limit: int = 50) -> dict:
    rows = await events.recent(conn, agent=agent_filter, project=project, kind=kind,
                               since=since, limit=limit)
    return {"events": [events.record_to_dict(r) for r in rows]}


async def digest(conn, agent, *, since: str | None = None, fmt: str = "json") -> dict | str:
    d = await make_digest(conn, since)
    if fmt == "md":
        return render_digest_md(d)
    return d


async def context(conn, agent, *, project: str | None = None) -> dict:
    return await make_context(conn, project=project, agent=agent["id"] if agent else None)


async def read(conn, agent, *, id: str) -> dict:
    """Fetch full artifact by id: E-<n>/D-<n>/L-<n> events, note/fact UUIDs (FR-7)."""
    id = id.strip()
    if id.startswith(("E-", "D-", "L-")):
        try:
            eid = int(id.split("-", 1)[1])
        except ValueError:
            raise KeyError(f"bad event id {id!r}")
        row = await events.get(conn, eid)
        if row is None:
            raise KeyError(f"event {id} not found")
        d = events.record_to_dict(row)
        d["adr"] = f"D-{eid}" if row["kind"] == "decision" else None
        if row["kind"] == "lesson":
            lr = await conn.fetchrow("SELECT * FROM lessons WHERE event_id = $1", eid)
            if lr:
                d["lesson"] = notes.lesson_to_dict(lr)
        return d
    n = await notes.get_note(conn, id)
    if n is not None:
        return notes.note_to_dict(n, with_body=True)
    f = await conn.fetchrow("SELECT * FROM facts WHERE id = $1", _uuid(id))
    if f is not None:
        return facts.fact_to_dict(f)
    raise KeyError(f"no artifact with id {id!r}")


async def entities(conn, agent, *, query: str | None = None, limit: int = 50) -> dict:
    rows = await conn.fetch(
        """
        SELECT e.*, count(f.id) AS fact_count FROM entities e
        LEFT JOIN facts f ON f.subj = e.id AND f.valid_to IS NULL
        WHERE ($1::text IS NULL OR lower(e.name) LIKE lower('%' || $1 || '%'))
        GROUP BY e.id ORDER BY fact_count DESC, e.name LIMIT $2
        """,
        query, min(limit, 200),
    )
    return {"entities": [
        {"id": str(r["id"]), "name": r["name"], "type": r["etype"],
         "summary": r["summary"], "facts": r["fact_count"]}
        for r in rows
    ]}


async def facts_about(conn, agent, *, entity: str, history: bool = False) -> dict:
    rows = await facts.facts_about(conn, entity, include_history=history)
    return {"entity": entity, "facts": [facts.fact_to_dict(r) for r in rows]}


async def graph(conn, agent, *, project: str | None = None, history: bool = False,
                limit: int = 300) -> dict:
    """Knowledge-graph view (FR-13): entities as nodes, facts as edges —
    subjects, tools/MCPs/apps in use, decisions, research links."""
    from ..graph import build_graph

    args: list[Any] = [min(limit, 1000)]
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
        *args,
    )
    args = [min(limit, 1000) * 4]
    if project:
        args.append(project)
        proj_where = f" AND (e.project = ${len(args)} OR e.project IS NULL)"
    else:
        proj_where = ""
    facts_rows = await conn.fetch(
        f"""
        SELECT f.* FROM facts f
        LEFT JOIN events e ON e.id = f.episode_id
        WHERE TRUE{proj_where}
        ORDER BY f.valid_from DESC
        LIMIT $1
        """,
        *args,
    )
    args_docs: list[Any] = [min(limit, 1000)]
    if project:
        args_docs.append(project)
        docs_proj_where = f" AND (project = ${len(args_docs)} OR project IS NULL)"
    else:
        docs_proj_where = ""
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
    g["stats"] = {"entities": len(g["nodes"]), "facts": len(g["edges"]),
                  "docs": sum(1 for n in g["nodes"] if n["group"] == "doc"),
                  "history": history}
    return g


async def agents_directory(conn, caller) -> dict:
    rows = await conn.fetch("SELECT id, name, harness, role, last_seen FROM agents ORDER BY id")
    return {"agents": [
        {"id": r["id"], "name": r["name"], "harness": r["harness"], "role": r["role"],
         "last_seen": r["last_seen"].isoformat() if r["last_seen"] else None}
        for r in rows
    ]}


def hook_idempotency_key(payload: dict, salt: str = "hook") -> str | None:
    """Deterministic idempotency for hook writes (FR-2 AC2)."""
    session_id = payload.get("session_id")
    hook_event = payload.get("hook_event_uuid") or payload.get("hook_event_name")
    if not (session_id and hook_event):
        return None
    raw = json.dumps({"s": session_id, "e": hook_event, "salt": salt},
                     sort_keys=True, default=str)
    return "hook-" + hashlib.sha256(raw.encode()).hexdigest()[:32]


def _uuid(s: str):
    import uuid as _uuid

    return _uuid.UUID(s)
