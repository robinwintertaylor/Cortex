"""Tool registry (FR-4 adjacent): what each agent can actually reach.

Tools were already arriving in the brain — as free-text entities the extractor
guessed at, split arbitrarily between etype 'tool' and 'tech', with `uses_tool`
appearing as a predicate a handful of times and never resolving to anything.
The information came in and dissolved.

Tools are one of the few naturally *closed* sets in this system: you can
enumerate every MCP server and app in a stack. Registering them gives the
extractor a controlled vocabulary to resolve against, which is what turns a
tool reference from a text literal into a real entity→entity edge.

Two rules keep this from becoming a CMDB:

  * **Declared, not extracted.** Agents report their own toolset at session
    start (deploy/constitution.md §1); nothing here is inferred from prose.
  * **Ambient state stays declarative.** Which servers are installed changes
    constantly, so the registry is a projection of the latest declaration per
    agent — not a stream of episodes. Actual tool *use* is what becomes an
    event, via the existing hook sink.

Like every projection, this is rebuilt from `tool_declaration` events by
`cortex rebuild --from 0`; nothing writes here without an event.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

import asyncpg

from .facts import get_or_create_entity, is_entity_like

KINDS = ("mcp_server", "app", "cli", "service")
DEFAULT_KIND = "mcp_server"


def normalize(raw: Iterable[Any]) -> list[dict[str, Any]]:
    """Clamp a declaration payload into safe, deduplicated tool records.

    Accepts either bare strings (`["Docker", "ripgrep"]`) or objects
    (`[{"name": ..., "kind": ..., "server": ..., "version": ...}]`) so a
    harness can declare cheaply and still be precise when it matters.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in list(raw or [])[:200]:
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()[:120]
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        kind = str(item.get("kind") or DEFAULT_KIND).strip().lower()
        out.append({
            "name": name,
            "kind": kind if kind in KINDS else DEFAULT_KIND,
            "server": (str(item.get("server")).strip()[:120]
                       if item.get("server") else None),
            "version": (str(item.get("version")).strip()[:40]
                        if item.get("version") else None),
        })
    return out


async def declare(conn: asyncpg.Connection, *, agent: str,
                  tools: Iterable[dict[str, Any]],
                  event_id: int | None = None,
                  replace: bool = True) -> list[asyncpg.Record]:
    """Write one agent's declared toolset.

    `replace=True` (the default) makes the declaration authoritative: tools the
    agent no longer reports are dropped, so an uninstalled MCP server stops
    showing up. The caller owns the transaction.
    """
    records = normalize(tools)
    names = [t["name"] for t in records]
    if replace:
        await conn.execute(
            "DELETE FROM tools WHERE agent = $1 AND NOT (lower(name) = ANY($2::text[]))",
            agent, [n.lower() for n in names],
        )
    out: list[asyncpg.Record] = []
    for t in records:
        # a registered tool is also an entity, so facts can point at it and it
        # shows up on the map alongside everything else it relates to
        entity_id = None
        if is_entity_like(t["name"]):
            ent = await get_or_create_entity(conn, t["name"], "tool")
            entity_id = ent["id"]
            if not ent["etype"]:
                await conn.execute(
                    "UPDATE entities SET etype = 'tool' WHERE id = $1", ent["id"])
        row = await conn.fetchrow(
            """
            INSERT INTO tools (agent, name, tool_kind, server, version, entity_id, event_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (agent, name) DO UPDATE SET
                tool_kind = EXCLUDED.tool_kind,
                server    = EXCLUDED.server,
                version   = EXCLUDED.version,
                entity_id = COALESCE(EXCLUDED.entity_id, tools.entity_id),
                event_id  = EXCLUDED.event_id,
                last_declared = now()
            RETURNING *
            """,
            agent, t["name"], t["kind"], t["server"], t["version"], entity_id, event_id,
        )
        out.append(row)
    return out


async def resolve(conn: asyncpg.Connection, name: str) -> asyncpg.Record | None:
    """Find a registered tool by name — the extractor's vocabulary lookup."""
    if not name or not name.strip():
        return None
    return await conn.fetchrow(
        "SELECT * FROM tools WHERE lower(name) = lower($1) "
        "ORDER BY last_declared DESC LIMIT 1",
        name.strip(),
    )


async def for_agent(conn: asyncpg.Connection, agent: str) -> list[asyncpg.Record]:
    return await conn.fetch(
        "SELECT * FROM tools WHERE agent = $1 ORDER BY tool_kind, lower(name)", agent)


async def registry(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    return await conn.fetch(
        "SELECT * FROM tools ORDER BY agent, tool_kind, lower(name)")


def tool_to_dict(r: asyncpg.Record) -> dict[str, Any]:
    return {
        "agent": r["agent"],
        "name": r["name"],
        "kind": r["tool_kind"],
        "server": r["server"],
        "version": r["version"],
        "entity_id": str(r["entity_id"]) if r["entity_id"] else None,
        "first_declared": _iso(r["first_declared"]),
        "last_declared": _iso(r["last_declared"]),
    }


def _iso(v) -> str | None:
    return v.isoformat() if isinstance(v, datetime) else None
