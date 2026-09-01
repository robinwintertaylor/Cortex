"""Markdown export (FR-14): `cortex export --markdown --out DIR`.

Exports the human-readable views — decisions/, lessons/, dossiers/, digests/ —
in a tree compatible with the Brainstem vault layout so it opens in Obsidian
with intact links (AC)."""

from __future__ import annotations

from pathlib import Path

import asyncpg

from .digest import digest as make_digest, render_digest_md
from .facts import fact_to_dict
from .util import parse_since, slugify


def _adr_md(event, fact) -> str:
    p = event["payload"] if isinstance(event["payload"], dict) else {}
    adr = p.get("adr") or f"D-{event['id']}"
    lines = [
        f"# {adr}: {p.get('title', '')}",
        "",
        f"- **Status**: {'superseded' if fact and fact['valid_to'] else 'active'}",
        f"- **Date**: {event['ts'].date().isoformat()}",
        f"- **Agent**: {event['agent']} ({event['harness'] or '?'})",
        f"- **Project**: {event['project'] or '-'}",
        f"- **Options**: {', '.join(p.get('options') or [])}",
        f"- **Choice**: **{p.get('choice', '')}**",
        "",
        "## Rationale",
        "",
        p.get("rationale") or "_none recorded_",
    ]
    if fact:
        f = fact_to_dict(fact)
        lines += ["", f"_Fact `{f['id']}` (pred `decided`, confidence {f['confidence']:.2f})._"]
    return "\n".join(lines)


def _lesson_md(row) -> str:
    lid = f"L-{str(row['id'])[:8]}"
    verified = f" (verified by {row['verified_by']})" if row["verified_by"] else ""
    return (
        f"# {lid}: {row['statement']}\n\n"
        f"- **Status**: {row['status']}\n"
        f"- **Project**: {row['project'] or '-'}\n"
        f"- **Confidence**: {row['confidence']:.2f}\n"
        f"- **Last verified**: {row['last_verified'].date().isoformat() if row['last_verified'] else '-'}{verified}\n"
    )


def _dossier_md(project, facts, decisions, lessons, recent) -> str:
    lines = [
        f"# Dossier: {project}",
        "",
        "## Current facts",
        "",
    ]
    lines += [f"- {f['subj_name']} **{f['predicate']}** {f['obj'] or ''} "
              f"(conf {f['confidence']:.2f})" for f in facts] or ["_none_"]
    lines += ["", "## Active decisions", ""]
    lines += [f"- [{d['adr']}] {d['title']} → **{d['choice']}**" for d in decisions] or ["_none_"]
    lines += ["", "## Lessons", ""]
    lines += [f"- {l['statement']}" for l in lessons] or ["_none_"]
    lines += ["", "## Recent activity", ""]
    lines += [f"- `{r['ts']}` **{r['agent']}** {r['kind']}: "
              f"{(r['payload'] or {}).get('summary', '')} [E#{r['id']}]"
              for r in recent][:30] or ["_none_"]
    return "\n".join(lines)


async def export_markdown(conn: asyncpg.Connection, out: Path, since_days: int = 7) -> dict:
    """Write the export tree. Returns a summary of files written."""
    out = Path(out)
    decisions_dir = out / "decisions"
    lessons_dir = out / "lessons"
    dossiers_dir = out / "dossiers"
    digests_dir = out / "digests"
    for d in (decisions_dir, lessons_dir, dossiers_dir, digests_dir):
        d.mkdir(parents=True, exist_ok=True)

    written: list[str] = []

    # decisions (from events, with their facts)
    events = await conn.fetch("SELECT * FROM events WHERE kind = 'decision' ORDER BY id")
    for ev in events:
        fact = await conn.fetchrow(
            "SELECT * FROM facts WHERE episode_id = $1 ORDER BY valid_from DESC LIMIT 1",
            ev["id"],
        )
        adr = (ev["payload"] or {}).get("adr") or f"D-{ev['id']}"
        title = (ev["payload"] or {}).get("title") or "decision"
        path = decisions_dir / f"{adr}-{slugify(title)}.md"
        path.write_text(_adr_md(ev, fact), encoding="utf-8")
        written.append(str(path))

    # lessons
    for row in await conn.fetch("SELECT * FROM lessons ORDER BY ts DESC"):
        path = lessons_dir / f"L-{str(row['id'])[:8]}-{slugify(row['statement'])}.md"
        path.write_text(_lesson_md(row), encoding="utf-8")
        written.append(str(path))

    # dossiers per project
    projects = [r["project"] for r in await conn.fetch(
        "SELECT DISTINCT project FROM events WHERE project IS NOT NULL")]
    for project in projects:
        facts_rows = await conn.fetch(
            "SELECT * FROM facts WHERE lower(subj_name) LIKE lower($1) "
            "AND valid_to IS NULL ORDER BY confidence DESC LIMIT 50",
            f"%{project}%",
        )
        decisions = await conn.fetch(
            "SELECT * FROM events WHERE kind='decision' AND project = $1 ORDER BY id DESC LIMIT 25",
            project,
        )
        dec_dicts = [
            {"adr": (d["payload"] or {}).get("adr") or f"D-{d['id']}",
             "title": (d["payload"] or {}).get("title", ""),
             "choice": (d["payload"] or {}).get("choice", "")}
            for d in decisions
        ]
        lessons = await conn.fetch(
            "SELECT * FROM lessons WHERE project = $1 AND status='active' ORDER BY confidence DESC LIMIT 15",
            project,
        )
        recent = await conn.fetch(
            "SELECT * FROM events WHERE project = $1 ORDER BY ts DESC LIMIT 30", project
        )
        p = dossiers_dir / f"{slugify(project)}.md"
        p.write_text(_dossier_md(project, [fact_to_dict(f) for f in facts_rows],
                                 dec_dicts, lessons, recent), encoding="utf-8")
        written.append(str(p))

    # digest archive (last `since_days` days, one file per day boundary)
    d = await make_digest(conn, f"{since_days}d")
    p = digests_dir / f"digest-{parse_since(f'{since_days}d').date()}.md"
    p.write_text(render_digest_md(d), encoding="utf-8")
    written.append(str(p))

    return {"files": written, "count": len(written)}
