"""Shared task queue (FR-12) + adjudication board (FR-5).

Queue mutations are themselves events (queue_claim / queue_complete), so queue
state is derivable from the log. Claims use a row lock so two agents can never
both claim a task (AC)."""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg

from .events import append
from .projects import canonical


async def add(
    conn: asyncpg.Connection,
    *,
    title: str,
    created_by: str,
    project: str | None = None,
    detail: dict[str, Any] | None = None,
    kind: str = "task",
    fact_id: str | None = None,
) -> asyncpg.Record:
    return await conn.fetchrow(
        """
        INSERT INTO queue (kind, title, detail, project, created_by, fact_id)
        VALUES ($1, $2, $3::jsonb, $4, $5, $6) RETURNING *
        """,
        kind, title, _dump(detail), canonical(project), created_by,
        uuid.UUID(fact_id) if fact_id else None,
    )


async def claim(conn: asyncpg.Connection, *, item_id: str, agent: str) -> asyncpg.Record | None:
    """Atomic claim: row lock via a single conditional UPDATE. Returns None when
    someone else got there first ('already claimed by X')."""
    row = await conn.fetchrow(
        """
        UPDATE queue SET status = 'claimed', claimed_by = $2, claimed_at = now()
        WHERE id = $1 AND status = 'open'
        RETURNING *
        """,
        uuid.UUID(item_id), agent,
    )
    return row


async def complete(
    conn: asyncpg.Connection, *, item_id: str, agent: str, outcome: str = "done"
) -> asyncpg.Record | None:
    status = "adjudicated" if outcome == "adjudicated" else "done"
    return await conn.fetchrow(
        """
        UPDATE queue SET status = $3
        WHERE id = $1 AND claimed_by = $2 AND status = 'claimed'
        RETURNING *
        """,
        uuid.UUID(item_id), agent, status,
    )


async def open_items(
    conn: asyncpg.Connection, *, kind: str | None = None, limit: int = 100
) -> list[asyncpg.Record]:
    where = ["status IN ('open', 'claimed')"]
    args: list[Any] = []
    if kind:
        args.append(kind)
        where.append(f"kind = ${len(args)}")
    args.append(limit)
    return await conn.fetch(
        f"SELECT * FROM queue WHERE {' AND '.join(where)} ORDER BY created DESC LIMIT ${len(args)}",
        *args,
    )


async def log_queue_event(
    conn: asyncpg.Connection, *, agent: str, kind: str, item: asyncpg.Record,
    harness: str | None = None, session: str | None = None,
) -> None:
    """Claims and completions are events (FR-2 kinds)."""
    assert kind in ("queue_claim", "queue_complete")
    await append(
        conn,
        agent=agent,
        kind=kind,
        harness=harness,
        session=session,
        project=item["project"],
        payload={"item_id": str(item["id"]), "kind": item["kind"], "title": item["title"]},
    )


def item_to_dict(r: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": str(r["id"]),
        "kind": r["kind"],
        "title": r["title"],
        "status": r["status"],
        "project": r["project"],
        "created_by": r["created_by"],
        "claimed_by": r["claimed_by"],
        "fact_id": str(r["fact_id"]) if r["fact_id"] else None,
        "detail": r["detail"],
        "created": _iso(r["created"]),
    }


def _dump(obj) -> str | None:
    import json

    return json.dumps(obj, default=str) if obj is not None else None


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else None
