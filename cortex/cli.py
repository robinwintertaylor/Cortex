"""Cortex CLI (FR-14, admin ops, rebuild).

  cortex migrate                        apply schema (idempotent)
  cortex admin genkey                   generate a fresh admin key / hook token
  cortex admin create-agent ID [opts]  register an agent, print key ONCE
  cortex admin list-agents
  cortex admin revoke ID
  cortex export --markdown --out DIR    Obsidian-compatible export
  cortex rebuild --from 0               replay the log into fresh projections
  cortex project-map [--force]          recompute semantic map coordinates
  cortex sweep URLS.txt                 capture a list of URLs (cron)
  cortex serve / dashboard              run the services (compose normally does)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
from pathlib import Path

from .config import get_config
from .log import setup_logging


async def cmd_migrate(args) -> int:
    from .db import get_pool, migrate

    await migrate()
    pool = await get_pool()
    await pool.close()
    print("schema applied.")
    return 0


async def cmd_create_agent(args) -> int:
    from .db import get_pool, migrate
    from .security import create_agent

    await migrate()
    pool = await get_pool()
    async with pool.acquire() as conn:
        _, key = await create_agent(
            conn, agent_id=args.id, name=args.name or args.id,
            harness=args.harness, role=args.role,
        )
    await pool.close()
    print(f"agent {args.id} registered. Store this key NOW — shown once:")
    print(f"  {key}")
    return 0


async def cmd_list_agents(args) -> int:
    from .db import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, name, harness, role, revoked, last_seen FROM agents ORDER BY id")
    await pool.close()
    for r in rows:
        print(f"{r['id']:<18} {r['role']:<7} harness={r['harness'] or '-':<14} "
              f"revoked={str(r['revoked']):<5} last_seen={r['last_seen']}")
    return 0


async def cmd_revoke(args) -> int:
    from .db import get_pool
    from .security import revoke_agent_key

    pool = await get_pool()
    async with pool.acquire() as conn:
        ok = await revoke_agent_key(conn, args.id)
    await pool.close()
    print(f"revoked {args.id}" if ok else f"agent {args.id} not found / already revoked")
    return 0 if ok else 1


async def cmd_genkey(args) -> int:
    print("cx-admin-" + secrets.token_urlsafe(24))
    return 0


async def cmd_export(args) -> int:
    from .db import get_pool
    from .export import export_markdown

    pool = await get_pool()
    async with pool.acquire() as conn:
        summary = await export_markdown(conn, Path(args.out), since_days=args.days)
    await pool.close()
    print(f"wrote {summary['count']} files to {args.out}/")
    for f in summary["files"]:
        print(f"  {f}")
    return 0


async def cmd_project_map(args) -> int:
    from . import mapproj
    from .db import get_pool
    from .librarian.worker import project_map

    n = await project_map(force=args.force)
    pool = await get_pool()
    await pool.close()
    if n == 0:
        print("map already current (use --force to reproject anyway).")
    else:
        print(f"projected {n} points using {mapproj.method()}.")
    return 0


async def cmd_rebuild(args) -> int:
    from .db import get_pool
    from .librarian.worker import rebuild

    try:
        from_id = int(args.from_id)
    except ValueError:
        print("--from must be an integer event id", file=sys.stderr)
        return 2
    await rebuild(from_id)
    return 0


async def cmd_sweep(args) -> int:
    from .db import get_pool
    from . import capture

    urls = [u.strip() for u in Path(args.urls).read_text().splitlines() if u.strip()]
    pool = await get_pool()
    async with pool.acquire() as conn:
        for url in urls:
            try:
                result = await capture.capture_url(conn, url=url, agent=args.agent or "sweep",
                                                    project=args.project)
                print(f"captured {url} → {result['note_id']}")
            except Exception as e:
                print(f"FAILED {url}: {e}", file=sys.stderr)
    await pool.close()
    return 0


async def cmd_serve(args) -> int:
    import uvicorn

    from .db import migrate

    await migrate()
    cfg = get_config()
    uvicorn.run("cortex.api.app:app", host=cfg.host, port=cfg.port, log_level="info")
    return 0


async def cmd_dashboard(args) -> int:
    import uvicorn

    uvicorn.run("cortex.dashboard:app", host=args.host or "0.0.0.0",
                port=int(args.port or 8740), log_level="info")
    return 0


def main(argv: list[str] | None = None) -> int:
    setup_logging("WARNING")
    p = argparse.ArgumentParser(prog="cortex", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("migrate", help="apply schema")

    a = sub.add_parser("admin", help="agent & key management")
    asub = a.add_subparsers(dest="admin", required=True)
    asub.add_parser("genkey", help="generate a fresh admin key")
    ca = asub.add_parser("create-agent", help="register agent, print key once")
    ca.add_argument("id")
    ca.add_argument("--name")
    ca.add_argument("--harness")
    ca.add_argument("--role", default="agent", choices=["agent", "owner", "admin"])
    asub.add_parser("list-agents")
    rv = asub.add_parser("revoke", help="revoke an agent's key")
    rv.add_argument("id")

    ex = sub.add_parser("export", help="markdown export")
    ex.add_argument("--markdown", action="store_true")
    ex.add_argument("--out", default="brain-export")
    ex.add_argument("--days", type=int, default=7)

    rb = sub.add_parser("rebuild", help="replay log into fresh projections")
    rb.add_argument("--from", dest="from_id", default="0")

    pm = sub.add_parser("project-map", help="recompute semantic map coordinates")
    pm.add_argument("--force", action="store_true",
                    help="reproject even when every point already has coordinates")

    sw = sub.add_parser("sweep", help="capture a list of URLs from a file")
    sw.add_argument("urls")
    sw.add_argument("--agent", default="sweep")
    sw.add_argument("--project")

    sv = sub.add_parser("serve", help="run the API (compose does this)")
    db = sub.add_parser("dashboard", help="run the dashboard")
    db.add_argument("--host")
    db.add_argument("--port")

    args = p.parse_args(argv)

    handlers = {
        "migrate": cmd_migrate,
        "admin": {
            "genkey": cmd_genkey, "create-agent": cmd_create_agent,
            "list-agents": cmd_list_agents, "revoke": cmd_revoke,
        # getattr, not args.admin: argparse only sets the nested subparser's
        # dest when `admin` actually ran, so reading it directly raised
        # AttributeError for every other subcommand — rebuild and migrate
        # included.
        }.get(getattr(args, "admin", None)),
        "export": cmd_export, "rebuild": cmd_rebuild,
        "project-map": cmd_project_map,
        "sweep": cmd_sweep, "serve": cmd_serve, "dashboard": cmd_dashboard,
    }
    h = handlers.get(args.cmd)
    if h is None:
        p.error(f"unknown command: {args.cmd} {getattr(args, 'admin', '')}")
    return asyncio.run(h(args))


if __name__ == "__main__":
    sys.exit(main())
