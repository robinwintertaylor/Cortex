"""MCP server (FR-7): streamable-HTTP endpoint at /mcp, mounted inside the
FastAPI app. Tool names are the stable Brainstem/Cortex/Hive set (NFR-8).

Identity: the MCP transport path resolves the caller from the bearer key in
middleware (see app.py) and exposes it via the `current_agent` contextvar —
tools never trust a self-declared agent name (FR-1 AC1)."""

from __future__ import annotations

from typing import Any

# MCP SDK v1 vs v2 compat (v2 renamed FastMCP → MCPServer and moved
# stateless_http to the app factory). Dual-era support keeps one codebase
# working with both — NFR-8.
try:
    from mcp.server.fastmcp import FastMCP  # mcp SDK v1

    _SDK_V2 = False
except ModuleNotFoundError:  # mcp SDK v2
    from mcp.server.mcpserver import MCPServer as FastMCP

    _SDK_V2 = True

from ..db import get_pool
from ..security import current_agent
from . import service

try:
    mcp = FastMCP("cortex", stateless_http=True)  # 2026-07-28-era stateless protocol
except TypeError:  # older/newer SDK without stateless flag at construction
    mcp = FastMCP("cortex")


def streamable_http_app():
    """Stateless streamable-HTTP ASGI app, wherever this SDK generation puts it."""
    if _SDK_V2:
        return mcp.streamable_http_app(stateless_http=True)
    return mcp.streamable_http_app()


class NoIdentity(RuntimeError):
    pass


def _agent() -> Any:
    agent = current_agent.get()
    if agent is None:
        raise NoIdentity("no agent identity — connect with Authorization: Bearer <agent key>")
    return agent


# ── bootstrap & awareness ──────────────────────────────────────────────────

@mcp.tool()
async def brain_context(project: str | None = None) -> dict:
    """Bootstrap bundle for a session: project dossier + active decisions +
    top lessons + recent events. Call this at session start."""
    agent = _agent()
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await service.context(conn, agent, project=project)


@mcp.tool()
async def brain_recent(agent_name: str | None = None, project: str | None = None,
                      since: str | None = None, limit: int = 50) -> dict:
    """Episodic awareness feed: what did the other agents do recently?
    `since` accepts '48h', '7d', or an ISO timestamp."""
    agent = _agent()
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await service.recent(conn, agent, agent_filter=agent_name,
                                    project=project, since=since, limit=limit)


@mcp.tool()
async def brain_digest(since: str | None = None) -> dict:
    """Grouped 'what changed' digest: actions by agent, new/superseded facts,
    new lessons, open queue. Every line cites event ids."""
    agent = _agent()
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await service.digest(conn, agent, since=since, fmt="json")


@mcp.tool()
async def brain_search(query: str, mode: str = "hybrid", type: str | None = None,
                       project: str | None = None, tag: str | None = None,
                       limit: int = 10) -> dict:
    """Hybrid semantic search (BM25 + vector + graph, RRF fusion). mode:
    'hybrid' (default), 'keyword', 'semantic', or 'rerank' (cross-encoder)."""
    agent = _agent()
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await service.search(conn, agent, query=query, mode=mode, type=type,
                                    project=project, tag=tag, limit=limit)


@mcp.tool()
async def brain_read(id: str) -> dict:
    """Fetch the full artifact: E-<n> / D-<n> / L-<n> events, note or fact UUID."""
    agent = _agent()
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await service.read(conn, agent, id=id)


# ── writes ──────────────────────────────────────────────────────────────────

@mcp.tool()
async def brain_log_action(summary: str, outcome: str | None = None,
                           files: list[str] | None = None, session: str | None = None,
                           project: str | None = None) -> dict:
    """Append an action event: what you did, outcome, files touched."""
    agent = _agent()
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await service.log_action(conn, agent, summary=summary, outcome=outcome,
                                        files=files, session=session, project=project)


@mcp.tool()
async def brain_log_decision(title: str, options: list[str], choice: str,
                             rationale: str | None = None,
                             project: str | None = None) -> dict:
    """Record a decision (ADR event + fact). Other agents will find and follow it."""
    agent = _agent()
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await service.log_decision(conn, agent, title=title, options=options,
                                          choice=choice, rationale=rationale,
                                          project=project)


@mcp.tool()
async def brain_lesson(statement: str, verified_by: str | None = None,
                       project: str | None = None) -> dict:
    """Record a durable lesson (heuristic, how-to, gotcha)."""
    agent = _agent()
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await service.lesson(conn, agent, statement=statement,
                                   verified_by=verified_by, project=project)


@mcp.tool()
async def brain_note(title: str, body: str, type: str = "note",
                     tags: list[str] | None = None, project: str | None = None) -> dict:
    """Create a note in the shared brain."""
    agent = _agent()
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await service.note(conn, agent, title=title, body=body, type=type,
                                  tags=tags, project=project)


@mcp.tool()
async def brain_capture_url(url: str, note: str | None = None) -> dict:
    """Research capture: fetch a URL (SSRF-guarded), extract the readable text,
    summarize, and store it as a tagged, searchable clipping."""
    agent = _agent()
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await service.capture_url(conn, agent, url=url, note_text=note)


@mcp.tool()
async def brain_upload_file(filename: str, content_base64: str,
                            project: str | None = None,
                            tags: list[str] | None = None) -> dict:
    """Upload a file (base64-encoded content) so every harness can find it
    later: stored content-addressed, text extracted best-effort (.txt/.md/
    .pdf/.docx) and made searchable/embedded like any other note. Download
    it back via GET /v1/brain_file/{note_id}."""
    agent = _agent()
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await service.upload_file(conn, agent, filename=filename,
                                         content_base64=content_base64,
                                         project=project, tags=tags)


# ── graph ───────────────────────────────────────────────────────────────────

@mcp.tool()
async def brain_entities(query: str | None = None) -> dict:
    """Entity directory (optionally filtered by name)."""
    agent = _agent()
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await service.entities(conn, agent, query=query)


@mcp.tool()
async def brain_facts_about(entity: str, history: bool = False) -> dict:
    """Current facts for an entity (full supersession history with history=True)."""
    agent = _agent()
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await service.facts_about(conn, agent, entity=entity, history=history)


@mcp.tool()
async def brain_graph(project: str | None = None, history: bool = False,
                      limit: int = 300) -> dict:
    """Knowledge-graph view: entities as nodes, facts as edges. Shows subjects,
    tools/MCPs/apps in use, decisions, and research links across the brain."""
    agent = _agent()
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await service.graph(conn, agent, project=project, history=history,
                                   limit=limit)


@mcp.tool()
async def brain_supersede_fact(fact_id: str, new_value: str,
                               rationale: str | None = None) -> dict:
    """Owner-authoritative supersession: close a fact and record its successor."""
    agent = _agent()
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            return await service.supersede_fact(conn, agent, fact_id=fact_id,
                                                new_value=new_value, rationale=rationale)
        except service.PermissionDenied as e:
            return {"error": str(e)}


# ── shared queue ────────────────────────────────────────────────────────────

@mcp.tool()
async def brain_queue_add(title: str, project: str | None = None) -> dict:
    """Add an open task to the shared queue."""
    agent = _agent()
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await service.queue_add(conn, agent, title=title, project=project)


@mcp.tool()
async def brain_queue_claim(item_id: str) -> dict:
    """Atomically claim an open task (first claimer wins)."""
    agent = _agent()
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await service.queue_claim(conn, agent, item_id=item_id)


@mcp.tool()
async def brain_queue_complete(item_id: str, outcome: str = "done") -> dict:
    """Complete a task you claimed."""
    agent = _agent()
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await service.queue_complete(conn, agent, item_id=item_id, outcome=outcome)


@mcp.tool()
async def brain_agents() -> dict:
    """Agent directory + last-seen: who shares this brain."""
    agent = _agent()
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await service.agents_directory(conn, agent)
