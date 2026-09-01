#!/usr/bin/env python3
"""upload-docs.py — upload a folder of markdown docs into the brain as notes.

Each file becomes one note (title = filename or first heading, body = file
content, tags + project as you choose). The librarian embeds them; hybrid
search (FTS covers the FULL body) makes them retrievable by every harness.

Credentials resolve like the `brain` wrapper: CORTEX_KEY env, else the key
file (CORTEX_KEY_FILE or ~/.config/cortex/agent-keys.key; CORTEX_AGENT picks
the line, default dsh). CORTEX_URL defaults to http://localhost:8738.

Usage:
  python3 scripts/upload-docs.py <folder> [--project second-brain]
                                  [--tag doc] [--glob '*.md'] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request

ROOT = os.path.expanduser("~/.config/cortex/agent-keys.key")


def resolve_key() -> str:
    if os.environ.get("CORTEX_KEY"):
        return os.environ["CORTEX_KEY"]
    kf = os.environ.get(
        "CORTEX_KEY_FILE",
        os.path.expanduser("~/.config/cortex/agent-keys.key"))
    want = os.environ.get("CORTEX_AGENT", "dsh")
    with open(kf) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            agent, key = line.split("=", 1)
            if agent == want:
                return key
    # fall back: bare key file (single cx-… line)
    with open(kf) as fh:
        for line in fh:
            line = line.strip()
            if line.startswith("cx-"):
                return line
    sys.exit(f"no key for agent '{want}' in {kf}")


def post(url: str, key: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url.rstrip("/") + "/v1/brain_note",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--project", default=None)
    ap.add_argument("--tag", action="append", default=[])
    ap.add_argument("--glob", default="*.md")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    url = os.environ.get("CORTEX_URL", "http://localhost:8738")
    key = resolve_key()

    import glob as globmod
    pattern = os.path.join(args.folder, "**", args.glob)
    files = sorted(set(globmod.glob(pattern, recursive=True)))
    if not files:
        sys.exit(f"no {args.glob} under {args.folder}")

    for path in files:
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
        m = re.search(r"^#\s+(.+)$", body, re.M)  # first H1 as title
        title = m.group(1).strip() if m else os.path.basename(path)
        title = f"{title} ({os.path.relpath(path, args.folder)})"
        payload = {"title": title[:500], "body": body,
                   "tags": args.tag, "project": args.project, "type": "note"}
        if args.dry_run:
            print(f"[dry-run] {title}  ({len(body):,} chars)")
            continue
        out = post(url, key, payload)
        print(f"✓ {os.path.relpath(path, args.folder):45s} "
              f"event E-{out['event_id']} note {out['note_id'][:8]} "
              f"({len(body):,} chars)")


if __name__ == "__main__":
    main()
