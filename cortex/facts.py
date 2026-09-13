"""Entities + bi-temporal facts (FR-4). Facts are never deleted, only superseded.

Supersession closes the old row (valid_to set, superseded_by link) and inserts
the new one in a single transaction (AC1). facts_about returns current facts by
default, full history with include_history=True (AC2)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import asyncpg

from .events import append  # noqa: F401  (re-exported for convenience)


# The closed vocabulary for entities.etype. graph.py's KNOWN_TYPES is the
# rendering set and also carries "doc", which is synthetic (notes/files render
# as nodes but are never entity rows) — this is what may actually be stored.
ENTITY_TYPES = ("agent", "tool", "tech", "concept", "project", "person", "other")


def _norm(text: str) -> str:
    return " ".join(text.strip().lower().split())


def _norm_pred(pred: str) -> str:
    import re

    p = re.sub(r"[^a-z0-9]+", "_", pred.strip().lower()).strip("_")
    return p or "related_to"


# A fact object only becomes a linked entity when it reads like a *name*, not a
# proposition. The extractor's object_is_entity flag is necessary but not
# sufficient — it happily marks whole clauses ("cortex local deployment is
# verified") as entities, and every one of those mints a junk entity that then
# anchors a junk region of the graph.
_CLAUSE_MARKERS = (" is ", " are ", " was ", " were ", " has ", " have ",
                   " had ", " will ", " would ", " should ", " must ",
                   " can ", " does ", " did ", " uses ", " needs ")


def is_entity_like(name: str | None) -> bool:
    """True when `name` can stand as an entity, not a sentence about one."""
    n = " ".join((name or "").strip().split())
    if not n or len(n) > 60 or len(n.split()) > 5:
        return False
    if n.endswith((".", "!", "?")) or any(c in n for c in ",;:"):
        return False
    return not any(m in f" {n.lower()} " for m in _CLAUSE_MARKERS)


async def get_or_create_entity(
    conn: asyncpg.Connection, name: str, etype: str | None = None
) -> asyncpg.Record:
    """Find entity by normalized-unique name (case-insensitive), create if missing."""
    row = await conn.fetchrow("SELECT * FROM entities WHERE lower(name) = lower($1)", name)
    if row:
        return row
    return await conn.fetchrow(
        "INSERT INTO entities (name, etype) VALUES ($1, $2) "
        "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING *",
        _norm(name) if etype is None else name.strip(),
        etype,
    )


async def add_fact(
    conn: asyncpg.Connection,
    *,
    subj_name: str,
    pred: str,
    obj_text: str | None = None,
    obj_entity: str | None = None,
    episode_id: int | None = None,
    kind: str = "extracted",
    confidence: float = 0.8,
    rationale: str | None = None,
    embedding=None,
) -> asyncpg.Record:
    subj = await get_or_create_entity(conn, subj_name)
    obj_id = None
    if obj_entity:
        obj = await get_or_create_entity(conn, obj_entity)
        obj_id = obj["id"]
    return await conn.fetchrow(
        """
        INSERT INTO facts (subj, subj_name, pred, obj, obj_text, episode_id,
                           kind, confidence, rationale, embedding)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        RETURNING *
        """,
        subj["id"], subj_name.strip(), _norm_pred(pred), obj_id,
        obj_text, episode_id, kind, confidence, rationale, embedding,
    )


async def supersede(
    conn: asyncpg.Connection,
    old_fact_id: str,
    *,
    new_value: str,
    rationale: str | None,
    episode_id: int | None = None,
    kind: str = "owner",
    confidence: float = 0.9,
    embedding=None,
    obj_entity: str | None = None,
) -> asyncpg.Record:
    """Close the old fact, add the successor — one transaction (AC1).

    The caller owns the transaction; this helper must run inside one."""
    old = await conn.fetchrow("SELECT * FROM facts WHERE id = $1", old_fact_id)
    if old is None:
        raise KeyError(f"fact {old_fact_id} not found")
    if old["valid_to"] is not None:
        raise ValueError(f"fact {old_fact_id} already superseded by {old['superseded_by']}")
    obj_id = None
    if obj_entity:
        obj_id = (await get_or_create_entity(conn, obj_entity))["id"]
    new = await conn.fetchrow(
        """
        INSERT INTO facts (subj, subj_name, pred, obj, obj_text, episode_id,
                           kind, confidence, rationale, embedding)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        RETURNING *
        """,
        old["subj"], old["subj_name"], old["pred"], obj_id, new_value, episode_id,
        kind, confidence, rationale, embedding,
    )
    await conn.execute(
        "UPDATE facts SET valid_to = now(), superseded_by = $1 WHERE id = $2",
        new["id"], old_fact_id,
    )
    return new


async def bump_confidence(conn: asyncpg.Connection, fact_id: str) -> None:
    await conn.execute(
        "UPDATE facts SET confidence = least(confidence + 0.05, 1.0) WHERE id = $1",
        fact_id,
    )


async def facts_about(
    conn: asyncpg.Connection, entity: str, *, include_history: bool = False
) -> list[asyncpg.Record]:
    """Current facts for an entity by default; full supersession chain with
    include_history (AC2)."""
    where = "" if include_history else " AND valid_to IS NULL"
    return await conn.fetch(
        f"""
        SELECT * FROM facts
        WHERE lower(subj_name) = lower($1){where}
        ORDER BY valid_from DESC
        """,
        entity,
    )


async def current_facts_for_pred(
    conn: asyncpg.Connection, subj_name: str, pred: str
) -> list[asyncpg.Record]:
    return await conn.fetch(
        """
        SELECT * FROM facts
        WHERE lower(subj_name) = lower($1) AND pred = $2 AND valid_to IS NULL
        ORDER BY valid_from DESC
        """,
        subj_name, pred,
    )


async def changed_since(
    conn: asyncpg.Connection, since: datetime
) -> list[asyncpg.Record]:
    """Facts created or superseded in the window — the 'what changed' query."""
    return await conn.fetch(
        """
        SELECT * FROM facts
        WHERE valid_from >= $1 OR (valid_to IS NOT NULL AND valid_to >= $1)
        ORDER BY COALESCE(valid_to, valid_from) DESC
        LIMIT 200
        """,
        since,
    )


async def supersession_chain(
    conn: asyncpg.Connection, subj_name: str, pred: str
) -> list[asyncpg.Record]:
    """Full supersession chain for a subject+predicate, oldest → newest."""
    return await conn.fetch(
        """
        SELECT * FROM facts
        WHERE lower(subj_name) = lower($1) AND pred = $2
        ORDER BY valid_from ASC
        """,
        subj_name, pred,
    )


async def active_decisions(
    conn: asyncpg.Connection, project: str | None = None
) -> list[asyncpg.Record]:
    """Decisions = currently-valid facts with pred='decided' (kind='decided').
    Project filter rides through the producing event (episode)."""
    q = """
        SELECT f.* FROM facts f
        LEFT JOIN events e ON e.id = f.episode_id
        WHERE f.pred = 'decided' AND f.valid_to IS NULL
    """
    args: list[Any] = []
    if project:
        args.append(project)
        q += f" AND (e.project = ${len(args)} OR f.subj_name = ${len(args)})"
    q += " ORDER BY f.valid_from DESC LIMIT 100"
    return await conn.fetch(q, *args)


def fact_to_dict(r: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": str(r["id"]),
        "subject": r["subj_name"],
        "predicate": r["pred"],
        "object": r["obj_text"],
        # set when the object resolved to a real entity rather than a literal —
        # the difference between a traversable edge and a dead-end leaf.
        "object_entity_id": str(r["obj"]) if r["obj"] else None,
        "kind": r["kind"],
        "confidence": r["confidence"],
        "rationale": r["rationale"],
        "valid_from": _iso(r["valid_from"]),
        "valid_to": _iso(r["valid_to"]),
        "superseded_by": str(r["superseded_by"]) if r["superseded_by"] else None,
        "episode_id": r["episode_id"],
        "status": "superseded" if r["valid_to"] is not None else "active",
    }


def _iso(v) -> str | None:
    return v.isoformat() if isinstance(v, datetime) else None


def now() -> datetime:
    return datetime.now(timezone.utc)
