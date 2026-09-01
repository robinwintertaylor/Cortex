#!/usr/bin/env python3
"""Audited admin purge for the event log (FR-2).

There is NO update/delete path in the service itself. This standalone script
is the single sanctioned purge tool: it is expected to be run by the owner,
reads every event it will touch into an audit file, and refuses to run without
--confirm.

  python -m scripts.purge_events --before 2025-01-01 --confirm
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cortex.db import get_pool  # noqa: E402


async def purge(before: str, confirm: bool, audit_path: str) -> int:
    from cortex.util import parse_since

    cutoff = parse_since(before)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM events WHERE ts < $1 ORDER BY id", cutoff)
        if not rows:
            print(f"no events before {cutoff.isoformat()}")
            return 0
        Path(audit_path).write_text(
            "\n".join(json.dumps(dict(r), default=str) for r in rows), encoding="utf-8")
        print(f"would purge {len(rows)} events; audit written to {audit_path}")
        if not confirm:
            print("dry run only — rerun with --confirm to actually purge")
            return 1
        await conn.execute("DELETE FROM events WHERE ts < $1", cutoff)
        print(f"purged {len(rows)} events")
    await pool.close()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--before", required=True, help="cutoff (ISO date or '1y')")
    ap.add_argument("--confirm", action="store_true")
    ap.add_argument("--audit", default="purge-audit.jsonl")
    args = ap.parse_args()
    sys.exit(asyncio.run(purge(args.before, args.confirm, args.audit)))
