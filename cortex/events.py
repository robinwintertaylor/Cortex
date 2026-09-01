"""Append-only event log (FR-2) + raw pre-extraction search (FR-3).

events is the single source of truth. There is no UPDATE or DELETE on this
table anywhere in Cortex — only INSERT and SELECT (see schema.py docstring).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import asyncpg

from .util import parse_since

KINDS = ("action", "decision", "lesson", "research", "note", "question",
          "queue_claim", "queue_complete")


class InvalidKind(ValueError):
    pass


def validate_kind(kind: str) -> str:
    if kind not in KINDS:
        raise InvalidKind(f"kind must be one of {KINDS}, got {kind!r}")
    return kind


async def append(
    conn: asyncpg.Connection,
    *,
    agent: str,
    kind: str,
    payload: dict[str, Any],
    harness: str | None = None,
    session: str | None = None,
    project: str | None = None,
    idempotency_key: str | None = None,
) -> asyncpg.Record:
    """Append one event. Idempotent when idempotency_key matches (FR-2 AC2).

    `agent` is the RESOLVED identity (from the bearer key server-side, FR-1);
    callers must never pass a self-declared agent through.
    """
    validate_kind(kind)
    payload_text = json.dumps(payload, default=str)
    row = await conn.fetchrow(
        """
        INSERT INTO events (agent, harness, session, kind, project, payload, idempotency_key)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING *
        """,
        agent, harness, session, kind, project, payload_text, idempotency_key,
    )
    if row is None and idempotency_key:
        # duplicate submission — return the original (stores once, AC2)
        row = await conn.fetchrow(
            "SELECT * FROM events WHERE idempotency_key = $1", idempotency_key
        )
    return row


def record_to_dict(r: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": r["id"],
        "ts": r["ts"].isoformat() if isinstance(r["ts"], datetime) else r["ts"],
        "agent": r["agent"],
        "harness": r["harness"],
        "session": r["session"],
        "kind": r["kind"],
        "project": r["project"],
        "payload": r["payload"],
    }


async def recent(
    conn: asyncpg.Connection,
    *,
    agent: str | None = None,
    project: str | None = None,
    kind: str | None = None,
    since: str | None = None,
    limit: int = 50,
) -> list[asyncpg.Record]:
    """Episodic awareness feed: brain_recent (O3)."""
    since_dt = parse_since(since, default_hours=24.0)
    where = ["ts >= $1"]
    args: list[Any] = [since_dt]
    if agent:
        args.append(agent)
        where.append(f"agent = ${len(args)}")
    if project:
        args.append(project)
        where.append(f"project = ${len(args)}")
    if kind:
        args.append(kind)
        where.append(f"kind = ${len(args)}")
    args.append(min(int(limit), 500))
    q = (
        f"SELECT * FROM events WHERE {' AND '.join(where)} "
        f"ORDER BY ts DESC, id DESC LIMIT ${len(args)}"
    )
    return await conn.fetch(q, *args)


async def get(conn: asyncpg.Connection, event_id: int) -> asyncpg.Record | None:
    return await conn.fetchrow("SELECT * FROM events WHERE id = $1", event_id)


async def fts_search(
    conn: asyncpg.Connection,
    query: str,
    *,
    limit: int = 50,
    agent: str | None = None,
    kind: str | None = None,
    project: str | None = None,
    since: str | None = None,
) -> list[tuple[asyncpg.Record, float]]:
    """Postgres full-text over payloads — day-one search before any extraction
    ran (FR-3). Returns (row, ts_rank) pairs."""
    where = ["payload_tsv @@ plainto_tsquery('simple', $1)"]
    args: list[Any] = [query]
    if agent:
        args.append(agent)
        where.append(f"agent = ${len(args)}")
    if kind:
        args.append(kind)
        where.append(f"kind = ${len(args)}")
    if project:
        args.append(project)
        where.append(f"project = ${len(args)}")
    if since:
        args.append(parse_since(since))
        where.append(f"ts >= ${len(args)}")
    args.append(limit)
    q = (
        f"SELECT *, ts_rank(payload_tsv, plainto_tsquery('simple', $1)) AS rank "
        f"FROM events WHERE {' AND '.join(where)} "
        f"ORDER BY rank DESC, ts DESC LIMIT ${len(args)}"
    )
    rows = await conn.fetch(q, *args)
    return [(r, r["rank"]) for r in rows]


async def count_since(conn: asyncpg.Connection, since: str | None) -> int:
    return await conn.fetchval(
        "SELECT count(*) FROM events WHERE ts >= $1", parse_since(since)
    )
