"""Notes (human/agent-authored artifacts + research captures) and lessons.

Notes are projections-with-provenance: every note records author + event link.
The librarian embeds notes asynchronously (embedding IS NULL until then)."""

from __future__ import annotations

from typing import Any

import asyncpg

from .util import dump_pg_json, slugify


async def create_note(
    conn: asyncpg.Connection,
    *,
    title: str,
    body: str,
    author: str,
    tags: list[str] | None = None,
    project: str | None = None,
    note_type: str = "note",
    source_url: str | None = None,
    fetch_date=None,
    event_id: int | None = None,
    derived: bool = False,
    links: list[str] | None = None,
) -> asyncpg.Record:
    return await conn.fetchrow(
        """
        INSERT INTO notes (title, body, author, tags, project, note_type,
                           source_url, fetch_date, event_id, derived, links)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        RETURNING *
        """,
        title, body, author, tags or [], project, note_type,
        source_url, fetch_date, event_id, derived, links or [],
    )


async def get_note(conn: asyncpg.Connection, note_id: str) -> asyncpg.Record | None:
    return await conn.fetchrow("SELECT * FROM notes WHERE id = $1", note_id)


async def fts_search(
    conn: asyncpg.Connection, query: str, *, limit: int = 50,
    project: str | None = None, tag: str | None = None,
) -> list[tuple[asyncpg.Record, float]]:
    where = ["to_tsvector('simple', title || ' ' || body) @@ plainto_tsquery('simple', $1)"]
    args: list[Any] = [query]
    if project:
        args.append(project)
        where.append(f"project = ${len(args)}")
    if tag:
        args.append(tag)
        where.append(f"${len(args)} = ANY(tags)")
    args.append(limit)
    q = (
        f"SELECT *, ts_rank(to_tsvector('simple', title || ' ' || body), "
        f"plainto_tsquery('simple', $1)) AS rank FROM notes "
        f"WHERE {' AND '.join(where)} ORDER BY rank DESC, ts DESC LIMIT ${len(args)}"
    )
    rows = await conn.fetch(q, *args)
    return [(r, r["rank"]) for r in rows]


async def ann_search(
    conn: asyncpg.Connection, qvec, *, limit: int = 50,
    project: str | None = None,
) -> list[tuple[asyncpg.Record, float]]:
    where = ["embedding IS NOT NULL"]
    args: list[Any] = [qvec]
    if project:
        args.append(project)
        where.append(f"project = ${len(args)}")
    args.append(limit)
    q = (
        f"SELECT *, 1 - (embedding <=> $1) AS sim FROM notes "
        f"WHERE {' AND '.join(where)} ORDER BY embedding <=> $1 LIMIT ${len(args)}"
    )
    rows = await conn.fetch(q, *args)
    return [(r, r["sim"]) for r in rows]


async def embed_pending(conn: asyncpg.Connection) -> int:
    """Called by the librarian: embed notes that lack embeddings (uses the
    service layer in librarian.worker; this returns candidates)."""
    return await conn.fetchval("SELECT count(*) FROM notes WHERE embedding IS NULL")


def note_to_dict(r: asyncpg.Record, *, with_body: bool = False) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": str(r["id"]),
        "type": r["note_type"],
        "title": r["title"],
        "tags": list(r["tags"] or []),
        "project": r["project"],
        "author": r["author"],
        "source_url": r["source_url"],
        "ts": _iso(r["ts"]),
    }
    if with_body:
        d["body"] = r["body"]
    else:
        d["snippet"] = r["body"][:280]
    return d


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else None


# ── lessons ─────────────────────────────────────────────────────────────────

async def add_lesson(
    conn: asyncpg.Connection,
    *,
    statement: str,
    verified_by: str | None = None,
    project: str | None = None,
    event_id: int | None = None,
    confidence: float = 0.8,
) -> asyncpg.Record:
    return await conn.fetchrow(
        """
        INSERT INTO lessons (statement, verified_by, project, event_id, confidence,
                             last_verified)
        VALUES ($1, $2, $3, $4, $5,
                CASE WHEN $2 IS NOT NULL THEN now() ELSE NULL END)
        RETURNING *
        """,
        statement, verified_by, project, event_id, confidence,
    )


async def top_lessons(
    conn: asyncpg.Connection, *, project: str | None = None, limit: int = 10
) -> list[asyncpg.Record]:
    """Top lessons ranked by recency × confidence (FR-10)."""
    args: list[Any] = []
    where = "status = 'active'"
    if project:
        args.append(project)
        where += f" AND (project = ${len(args)} OR project IS NULL)"
    args.append(limit)
    return await conn.fetch(
        f"""
        SELECT * FROM lessons WHERE {where}
        ORDER BY confidence * exp(- extract(epoch FROM (now() - ts)) / (86400.0 * 30.0)) DESC
        LIMIT ${len(args)}
        """,
        *args,
    )


async def lessons_since(
    conn: asyncpg.Connection, since
) -> list[asyncpg.Record]:
    return await conn.fetch(
        "SELECT * FROM lessons WHERE ts >= $1 ORDER BY ts DESC LIMIT 200", since
    )


def lesson_to_dict(r: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": str(r["id"]),
        "statement": r["statement"],
        "verified_by": r["verified_by"],
        "project": r["project"],
        "confidence": r["confidence"],
        "status": r["status"],
        "ts": _iso(r["ts"]),
    }


__all__ = [
    "create_note", "get_note", "fts_search", "ann_search", "note_to_dict",
    "add_lesson", "top_lessons", "lessons_since", "lesson_to_dict", "slugify",
    "dump_pg_json",
]
