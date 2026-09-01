"""Digest & context generation (FR-10).

brain_digest(since) renders a grouped, cited 'what changed' briefing:
actions by agent, new decisions, new/superseded facts, new lessons, open queue.
brain_context(project) renders a bootstrap bundle ≤ 25 KB (digest slice +
dossier + active decisions + top lessons by recency×confidence + recent events).
Every line cites event ids (provenance)."""

from __future__ import annotations

from typing import Any

import asyncpg

from .config import get_config
from .events import recent
from .facts import active_decisions, changed_since, fact_to_dict
from .notes import lessons_since, top_lessons
from .queue import open_items
from .util import parse_since, trunc


async def digest(conn: asyncpg.Connection, since: str | None = None) -> dict[str, Any]:
    since_dt = parse_since(since, default_hours=48.0)

    actions = await recent(conn, kind="action", since=since, limit=100)
    decisions = await recent(conn, kind="decision", since=since, limit=50)
    questions = await recent(conn, kind="question", since=since, limit=25)
    facts_changed = await changed_since(conn, since_dt)
    lessons = await lessons_since(conn, since_dt)
    queue_open = await open_items(conn)

    by_agent: dict[str, list[dict]] = {}
    for r in actions:
        by_agent.setdefault(r["agent"], []).append({
            "event_id": r["id"],
            "ts": r["ts"].isoformat(),
            "summary": (r["payload"] or {}).get("summary", ""),
            "outcome": (r["payload"] or {}).get("outcome", ""),
            "project": r["project"],
        })

    new_facts = [fact_to_dict(f) for f in facts_changed if f["valid_from"] >= since_dt]
    superseded_facts = [
        {**fact_to_dict(f), "superseded_at": f["valid_to"].isoformat()}
        for f in facts_changed
        if f["valid_to"] is not None and f["valid_to"] >= since_dt
    ]

    return {
        "since": since_dt.isoformat(),
        "actions_by_agent": by_agent,
        "new_decisions": [
            {
                "event_id": r["id"],
                "adr": (r["payload"] or {}).get("adr") or f"D-{r['id']}",
                "title": (r["payload"] or {}).get("title", ""),
                "choice": (r["payload"] or {}).get("choice", ""),
                "agent": r["agent"],
                "project": r["project"],
            }
            for r in decisions
        ],
        "new_facts": new_facts,
        "superseded_facts": superseded_facts,
        "new_lessons": [
            {"id": str(r["id"]), "statement": r["statement"], "project": r["project"]}
            for r in lessons
        ],
        "open_questions": [
            {"event_id": r["id"], "title": (r["payload"] or {}).get("title", ""), "agent": r["agent"]}
            for r in questions
        ],
        "open_queue": [
            {"id": str(r["id"]), "kind": r["kind"], "title": r["title"], "status": r["status"]}
            for r in queue_open
        ],
    }


def render_digest_md(d: dict[str, Any]) -> str:
    """Markdown rendering with event-id citations — used by SessionStart hooks."""
    lines: list[str] = [f"# Brain digest — changes since {d['since']}", ""]

    if d["actions_by_agent"]:
        lines.append("## Actions")
        for agent, items in sorted(d["actions_by_agent"].items()):
            lines.append(f"\n### {agent}")
            for it in items[:10]:
                out = f" — {it['outcome']}" if it.get("outcome") else ""
                lines.append(f"- {trunc(it['summary'], 140)}{out} [E#{it['event_id']}]")
        lines.append("")

    if d["new_decisions"]:
        lines.append("## New decisions")
        for x in d["new_decisions"]:
            lines.append(f"- **{x['adr']}: {x['title']}** → {x['choice']} (by {x['agent']}) [E#{x['event_id']}]")
        lines.append("")

    if d["new_facts"]:
        lines.append("## New facts")
        for f in d["new_facts"][:30]:
            lines.append(f"- {f['subject']} {f['predicate']} {f['object']} "
                         f"(conf {f['confidence']:.2f}) [F#{f['id'][:8]}]")
        lines.append("")

    if d["superseded_facts"]:
        lines.append("## Superseded facts")
        for f in d["superseded_facts"][:20]:
            lines.append(f"- {f['subject']} {f['predicate']} {f['object']} — superseded at {f['superseded_at']}")
        lines.append("")

    if d["new_lessons"]:
        lines.append("## New lessons")
        for l in d["new_lessons"]:
            lines.append(f"- {trunc(l['statement'], 160)}")
        lines.append("")

    if d["open_queue"]:
        lines.append("## Open queue")
        for q in d["open_queue"]:
            lines.append(f"- [{q['kind']}] {q['title']} ({q['status']})")
        lines.append("")

    if not any([d["actions_by_agent"], d["new_decisions"], d["new_facts"],
                d["superseded_facts"], d["new_lessons"], d["open_queue"]]):
        lines.append("_Nothing changed in this window._")

    return "\n".join(lines)


async def context(conn: asyncpg.Connection, project: str | None = None,
                  agent: str | None = None) -> dict[str, Any]:
    """Bootstrap bundle (≤ context_max_bytes): dossier + active decisions +
    top lessons + recent events. Used at session start (US-1)."""
    decisions = await active_decisions(conn, project)
    lessons = await top_lessons(conn, project=project, limit=8)
    evs = await recent(conn, project=project, limit=20)

    dossier: dict[str, Any] = {"project": project or "(global)", "facts": []}
    if project:
        ents = await conn.fetch(
            "SELECT * FROM entities WHERE lower(name) LIKE lower($1) LIMIT 5", f"%{project}%"
        )
        for e in ents:
            fs = await conn.fetch(
                "SELECT * FROM facts WHERE subj = $1 AND valid_to IS NULL "
                "ORDER BY confidence DESC LIMIT 15",
                e["id"],
            )
            dossier["facts"].extend(fact_to_dict(f) for f in fs)

    out = {
        "dossier": dossier,
        "active_decisions": [
            {
                "adr": (e["payload"] or {}).get("adr") or f"D-{e['id']}",
                "subject": f["subj_name"],
                "choice": f["obj_text"],
                "rationale": f["rationale"],
                "event_id": f["episode_id"],
            }
            for f in decisions
        ],
        "top_lessons": [
            {"statement": l["statement"], "confidence": l["confidence"], "project": l["project"]}
            for l in lessons
        ],
        "recent_events": [
            {
                "id": r["id"],
                "agent": r["agent"],
                "kind": r["kind"],
                "summary": trunc((r["payload"] or {}).get("summary") or
                                  (r["payload"] or {}).get("title") or str(r["payload"]), 120),
                "ts": r["ts"].isoformat(),
            }
            for r in evs
        ],
    }

    # hard cap (FR-10: ≤ 25 KB) — drop recent events first, then lessons
    import json as _json

    cfg = get_config()
    while len(_json.dumps(out, default=str).encode()) > cfg.context_max_bytes:
        if out["recent_events"]:
            out["recent_events"].pop()
        elif len(out["dossier"]["facts"]) > 10:
            out["dossier"]["facts"].pop()
        elif out["top_lessons"]:
            out["top_lessons"].pop()
        else:
            break
    return out
