"""REST API /v1/* (FR-8) — mirrors every MCP op 1:1 — plus hook sinks, health
and metrics. Responses follow the normative shapes in PRD §9."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from prometheus_client import generate_latest

from .. import events, metrics
from ..config import get_config
from ..security import create_agent, revoke_agent_key
from . import service
from .deps import get_conn, require_agent, require_admin, require_hook_token

router = APIRouter()


# ── health / metrics ────────────────────────────────────────────────────────

@router.get("/healthz")
async def healthz(conn=Depends(get_conn)):
    await conn.fetchval("SELECT 1")
    lag = await conn.fetchval(
        """
        SELECT COALESCE(max(extract(epoch FROM now() - e.ts)), 0) FROM events e
        LEFT JOIN librarian_state s ON s.event_id = e.id WHERE s.event_id IS NULL
        """
    )
    cfg = get_config()
    return {
        "ok": True,
        "db": "up",
        "llm": "enabled" if cfg.llm_enabled else "disabled-until-configured",
        "librarian_lag_seconds": round(float(lag or 0), 1),
    }


@router.get("/metrics")
async def prometheus_metrics(conn=Depends(get_conn)):
    try:
        open_q = await conn.fetchval("SELECT count(*) FROM queue WHERE status='open'")
        adj = await conn.fetchval(
            "SELECT count(*) FROM queue WHERE status='open' AND kind='adjudication'")
        metrics.queue_depth.labels("task").set((open_q or 0) - (adj or 0))
        metrics.queue_depth.labels("adjudication").set(adj or 0)
    except Exception:
        pass
    return Response(generate_latest(), media_type="text/plain")


# ── reads (GET + POST accepted on every op) ────────────────────────────────

async def _search(conn, agent, p: dict) -> dict:
    return await service.search(
        conn, agent,
        query=p.get("query") or p.get("q") or "",
        mode=p.get("mode") or "hybrid", type=p.get("type"),
        project=p.get("project"), tag=p.get("tag"), since=p.get("since"),
        limit=int(p.get("limit") or 10),
    )


@router.post("/v1/brain_search")
async def brain_search_post(body: dict, agent=Depends(require_agent), conn=Depends(get_conn)):
    return await _search(conn, agent, body)


@router.get("/v1/brain_search")
async def brain_search_get(q: str, mode: str = "hybrid", type: str | None = None,
                           project: str | None = None, tag: str | None = None,
                           since: str | None = None, limit: int = 10,
                           agent=Depends(require_agent), conn=Depends(get_conn)):
    return await _search(conn, agent, {"query": q, "mode": mode, "type": type,
                                       "project": project, "tag": tag, "since": since,
                                       "limit": limit})


@router.post("/v1/brain_recent")
async def brain_recent_post(body: dict, agent=Depends(require_agent), conn=Depends(get_conn)):
    return await service.recent(conn, agent, agent_filter=body.get("agent"),
                                project=body.get("project"), kind=body.get("kind"),
                                since=body.get("since"), limit=int(body.get("limit") or 50))


@router.get("/v1/brain_recent")
async def brain_recent_get(agent_filter: str | None = Query(default=None, alias="agent"),
                           project: str | None = None, kind: str | None = None,
                           since: str | None = None, limit: int = 50,
                           agent=Depends(require_agent), conn=Depends(get_conn)):
    return await service.recent(conn, agent, agent_filter=agent_filter, project=project,
                                kind=kind, since=since, limit=limit)


@router.post("/v1/brain_digest")
async def brain_digest_post(body: dict, agent=Depends(require_agent), conn=Depends(get_conn)):
    return await service.digest(conn, agent, since=body.get("since"), fmt=body.get("format", "json"))


@router.get("/v1/brain_digest")
async def brain_digest_get(since: str | None = None, fmt: str = "json",
                           agent=Depends(require_agent), conn=Depends(get_conn)):
    d = await service.digest(conn, agent, since=since, fmt=fmt)
    if fmt == "md":
        return PlainTextResponse(d)
    return d


@router.post("/v1/brain_context")
async def brain_context_post(body: dict = None, agent=Depends(require_agent), conn=Depends(get_conn)):
    return await service.context(conn, agent, project=(body or {}).get("project"))


@router.get("/v1/brain_context")
async def brain_context_get(project: str | None = None, agent=Depends(require_agent),
                            conn=Depends(get_conn)):
    return await service.context(conn, agent, project=project)


@router.get("/v1/brain_read/{item_id}")
async def brain_read(item_id: str, agent=Depends(require_agent), conn=Depends(get_conn)):
    try:
        return await service.read(conn, agent, id=item_id)
    except KeyError as e:
        raise HTTPException(404, str(e))


@router.post("/v1/brain_read")
async def brain_read_post(body: dict, agent=Depends(require_agent), conn=Depends(get_conn)):
    try:
        return await service.read(conn, agent, id=body.get("id", ""))
    except KeyError as e:
        raise HTTPException(404, str(e))


@router.get("/v1/brain_entities")
async def brain_entities(query: str | None = None, limit: int = 50,
                         agent=Depends(require_agent), conn=Depends(get_conn)):
    return await service.entities(conn, agent, query=query, limit=limit)


@router.get("/v1/brain_facts_about")
async def brain_facts_about(entity: str, history: bool = False,
                            agent=Depends(require_agent), conn=Depends(get_conn)):
    return await service.facts_about(conn, agent, entity=entity, history=history)


@router.get("/v1/brain_graph")
@router.post("/v1/brain_graph")
async def brain_graph(project: str | None = None, history: bool = False,
                     limit: int = 300, agent=Depends(require_agent),
                     conn=Depends(get_conn)):
    """Knowledge-graph view: entities as nodes, facts as edges (FR-13)."""
    return await service.graph(conn, agent, project=project, history=history,
                               limit=limit)


@router.get("/v1/brain_agents")
async def brain_agents(agent=Depends(require_agent), conn=Depends(get_conn)):
    return await service.agents_directory(conn, agent)


# ── writes ──────────────────────────────────────────────────────────────────

@router.post("/v1/brain_log_action")
async def brain_log_action(body: dict, agent=Depends(require_agent), conn=Depends(get_conn)):
    with metrics.append_latency.time():
        metrics.events_appended.labels("action").inc()
        return await service.log_action(
            conn, agent, summary=body.get("summary", ""), outcome=body.get("outcome"),
            files=body.get("files"), session=body.get("session"),
            project=body.get("project"),
            idempotency_key=body.get("idempotency_key"),
        )


@router.post("/v1/brain_log_decision")
async def brain_log_decision(body: dict, agent=Depends(require_agent), conn=Depends(get_conn)):
    metrics.events_appended.labels("decision").inc()
    return await service.log_decision(
        conn, agent, title=body.get("title", ""), options=body.get("options", []),
        choice=body.get("choice", ""), rationale=body.get("rationale"),
        project=body.get("project"), session=body.get("session"),
        idempotency_key=body.get("idempotency_key"),
    )


@router.post("/v1/brain_lesson")
async def brain_lesson(body: dict, agent=Depends(require_agent), conn=Depends(get_conn)):
    metrics.events_appended.labels("lesson").inc()
    return await service.lesson(
        conn, agent, statement=body.get("statement", ""),
        verified_by=body.get("verified_by"), project=body.get("project"),
        session=body.get("session"),
    )


@router.post("/v1/brain_note")
async def brain_note(body: dict, agent=Depends(require_agent), conn=Depends(get_conn)):
    metrics.events_appended.labels("note").inc()
    return await service.note(
        conn, agent, title=body.get("title", ""), body=body.get("body", ""),
        type=body.get("type", "note"), tags=body.get("tags"),
        project=body.get("project"), session=body.get("session"),
    )


@router.post("/v1/brain_capture_url")
async def brain_capture_url(body: dict, agent=Depends(require_agent), conn=Depends(get_conn)):
    from ..capture import SsrfBlocked

    try:
        return await service.capture_url(
            conn, agent, url=body.get("url", ""), note_text=body.get("note"),
            session=body.get("session"), project=body.get("project"),
        )
    except SsrfBlocked as e:
        # AC1: refused AND logged
        import logging

        logging.getLogger("cortex.capture").warning("SSRF blocked: %s", e)
        raise HTTPException(400, f"blocked: {e}")
    except Exception as e:
        raise HTTPException(502, f"capture failed: {e}")


@router.post("/v1/brain_upload_file")
async def brain_upload_file(body: dict, agent=Depends(require_agent), conn=Depends(get_conn)):
    try:
        return await service.upload_file(
            conn, agent, filename=body.get("filename", ""),
            content_base64=body.get("content_base64", ""),
            project=body.get("project"), tags=body.get("tags"),
            session=body.get("session"),
        )
    except service.UploadTooLarge as e:
        raise HTTPException(413, str(e))
    except Exception as e:
        raise HTTPException(400, f"upload failed: {e}")


@router.get("/v1/brain_file/{note_id}")
async def brain_file(note_id: str, agent=Depends(require_agent), conn=Depends(get_conn)):
    from .. import files as blobstore

    row = await conn.fetchrow(
        "SELECT * FROM notes WHERE id = $1 AND note_type = 'document'", note_id)
    if row is None or not row["storage_path"]:
        raise HTTPException(404, "no such file")
    content = blobstore.read_blob(row["storage_path"])
    filename = row["original_filename"] or "download"
    return Response(
        content=content, media_type=row["mime_type"] or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/v1/brain_supersede_fact")
async def brain_supersede_fact(body: dict, agent=Depends(require_agent), conn=Depends(get_conn)):
    try:
        return await service.supersede_fact(
            conn, agent, fact_id=body.get("fact_id", ""),
            new_value=body.get("new_value", ""), rationale=body.get("rationale"),
        )
    except service.PermissionDenied as e:
        raise HTTPException(403, str(e))
    except (KeyError, ValueError) as e:
        raise HTTPException(404, str(e))


@router.post("/v1/brain_sweep")
async def brain_sweep(body: dict, agent=Depends(require_agent), conn=Depends(get_conn)):
    """Nightly research sweep for cron (FR-11): list of URLs."""
    return {"captures": await service.sweep(conn, agent, body.get("urls", []),
                                            project=body.get("project"))}


# ── queue (FR-12) ────────────────────────────────────────────────────────────

@router.post("/v1/brain_queue_add")
async def brain_queue_add(body: dict, agent=Depends(require_agent), conn=Depends(get_conn)):
    return await service.queue_add(conn, agent, title=body.get("title", ""),
                                  project=body.get("project"), detail=body.get("detail"))


@router.get("/v1/brain_queue")
async def brain_queue_open(agent=Depends(require_agent), conn=Depends(get_conn)):
    from ..queue import item_to_dict, open_items

    rows = await open_items(conn)
    return {"items": [item_to_dict(r) for r in rows]}


@router.post("/v1/brain_queue_claim")
async def brain_queue_claim(body: dict, agent=Depends(require_agent), conn=Depends(get_conn)):
    return await service.queue_claim(conn, agent, item_id=body.get("item_id", ""))


@router.post("/v1/brain_queue_complete")
async def brain_queue_complete(body: dict, agent=Depends(require_agent), conn=Depends(get_conn)):
    return await service.queue_complete(conn, agent, item_id=body.get("item_id", ""),
                                        outcome=body.get("outcome", "done"))


# ── hook sinks (FR-8, Claude Code HTTP hooks) ──────────────────────────────

def _hook_event_from_payload(p: dict) -> tuple[str, dict, str | None]:
    """Map Claude Code hook JSON → (kind, payload, idempotency_key)."""
    tool = p.get("tool_name") or "unknown"
    tool_input = p.get("tool_input") or {}
    hook_event = p.get("hook_event_name") or "hook"
    summary_map = {
        "PostToolUse": f"used {tool}",
        "SessionEnd": "session ended",
        "UserPromptSubmit": "prompt submitted",
        "Stop": "turn completed",
    }
    summary = summary_map.get(hook_event, f"{hook_event} {tool}")
    interesting = {k: v for k, v in tool_input.items() if k in
                   ("command", "path", "file_path", "pattern", "url", "description")}
    payload = {
        "summary": summary,
        "outcome": "auto-captured",
        "tool_name": tool,
        "tool_input": interesting,
        "cwd": p.get("cwd"),
        "hook_event_name": hook_event,
    }
    response = p.get("tool_response")
    if response is not None:
        payload["tool_response_brief"] = str(response)[:2000]
    return "action", payload, service.hook_idempotency_key(p)


@router.post("/hook/tool-use")
async def hook_tool_use(request: Request, hook=Depends(require_hook_token), conn=Depends(get_conn)):
    p = await request.json()
    declared_agent = str(p.get("agent") or request.query_params.get("agent") or "claude-code")
    kind, payload, idem = _hook_event_from_payload(p)
    row = await events.append(
        conn,
        agent=declared_agent, kind=kind, harness=p.get("harness", "claude-code"),
        session=p.get("session_id"), project=p.get("project") or None,
        idempotency_key=idem, payload=payload,
    )
    metrics.events_appended.labels("action").inc()
    return {"ok": True, "event_id": row["id"]}


@router.post("/hook/session-end")
async def hook_session_end(request: Request, hook=Depends(require_hook_token), conn=Depends(get_conn)):
    p = await request.json()
    reason = p.get("reason") or "end"
    declared_agent = str(p.get("agent") or request.query_params.get("agent") or "claude-code")
    payload = {"summary": f"session ended ({reason})", "outcome": "auto",
               "cwd": p.get("cwd"), "reason": reason}
    await events.append(
        conn,
        agent=declared_agent, kind="action", harness=p.get("harness", "claude-code"),
        session=p.get("session_id"), project=p.get("project") or None,
        idempotency_key=service.hook_idempotency_key(p), payload=payload,
    )
    metrics.events_appended.labels("action").inc()
    return {"ok": True}


# ── admin (agent registry, FR-1) ─────────────────────────────────────────────

@router.post("/v1/admin/agents")
async def admin_create_agent(body: dict, admin=Depends(require_admin), conn=Depends(get_conn)):
    """Create an agent + return its plaintext key ONCE."""
    async with conn.transaction():
        try:
            _id, key = await create_agent(
                conn,
                agent_id=body["id"],
                name=body.get("name", body["id"]),
                harness=body.get("harness"),
                role=body.get("role", "agent"),
            )
        except (KeyError, ValueError) as e:
            raise HTTPException(400, str(e))
    return {"agent_id": _id, "key": key,
            "note": "store this key now — it is never shown again"}


@router.get("/v1/admin/agents")
async def admin_list_agents(admin=Depends(require_admin), conn=Depends(get_conn)):
    rows = await conn.fetch(
        "SELECT id, name, harness, role, revoked, last_seen, created FROM agents ORDER BY id")
    return {"agents": [dict(r) for r in rows]}


@router.delete("/v1/admin/agents/{agent_id}/key")
async def admin_revoke_key(agent_id: str, admin=Depends(require_admin), conn=Depends(get_conn)):
    ok = await revoke_agent_key(conn, agent_id)
    if not ok:
        raise HTTPException(404, f"agent {agent_id} not found or already revoked")
    return {"agent_id": agent_id, "revoked": True}
