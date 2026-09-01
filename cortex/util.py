"""Small shared helpers."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

_REL = re.compile(r"^(\d+)\s*(s|m|h|d|w)$", re.I)
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_since(since: str | None, default_hours: float = 48.0) -> datetime:
    """Accept '48h', '7d', ISO-8601, or epoch seconds. Returns UTC datetime."""
    if not since:
        return datetime.now(timezone.utc) - timedelta(hours=default_hours)
    s = since.strip()
    if (m := _REL.match(s)):
        delta = timedelta(seconds=int(m.group(1)) * _UNITS[m.group(2).lower()])
        return datetime.now(timezone.utc) - delta
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        pass
    try:
        return datetime.fromtimestamp(int(s), tz=timezone.utc)
    except (ValueError, OSError):
        raise ValueError(f"cannot parse since={since!r}")


def slugify(text: str, maxlen: int = 60) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return s[:maxlen].rstrip("-") or "untitled"


def dump_pg_json(obj: Any) -> str:
    """asyncpg wants str for jsonb unless codec registered; we pass explicit text."""
    return json.dumps(obj, default=str)


def payload_dict(value: Any) -> dict[str, Any]:
    """Normalize event payload. asyncpg returns jsonb as str without a codec."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode()
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def trunc(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"
