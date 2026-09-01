"""Research capture (FR-11): brain_capture_url.

Fetch pipeline: SSRF guard → fetch (15 s timeout, 5 MB cap, manual redirect
re-validation) → readability extraction (trafilatura) → LLM summary (or local
fallback) → store as note + event + enqueue for librarian extraction, with
source_url and fetch_date preserved. Internal/private IPs are refused AND
logged (AC1). The captured page is retrievable by meaning within 1 min (AC2):
the event + note are searchable immediately (FTS) and embedded by the librarian
within its lag bound."""

from __future__ import annotations

import ipaddress
import socket
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import asyncpg
import httpx

from . import events, notes
from .config import get_config
from .llm import summarize_text
from .log import get_logger
from .util import trunc

log = get_logger(__name__)

MAX_REDIRECTS = 3
ALLOWED_SCHEMES = ("http", "https")


class SsrfBlocked(ValueError):
    """Raised (and logged) when a URL resolves to a private/reserved IP (AC1)."""


def assert_public_url(url: str) -> None:
    """Validate scheme + resolve hostnames and refuse private/link-local IPs.

    Checks EVERY address the hostname resolves to (DNS rebinding mitigation:
    if any A/AAAA record is private, refuse)."""
    p = urlparse(url)
    if p.scheme not in ALLOWED_SCHEMES:
        raise SsrfBlocked(f"scheme {p.scheme!r} not allowed")
    host = p.hostname
    if not host:
        raise SsrfBlocked("no hostname")
    try:
        ip = ipaddress.ip_address(host)
        candidates = [ip]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror as e:
            raise SsrfBlocked(f"cannot resolve {host!r}: {e}") from e
        candidates = [ipaddress.ip_address(i[4][0]) for i in infos]
    for ip in candidates:
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
            or (ip.version == 6 and (ip.ipv4_mapped and ip.ipv4_mapped.is_private))
        ):
            raise SsrfBlocked(f"{host} resolves to private/reserved address {ip}")


async def fetch_page(url: str) -> tuple[str, str]:
    """Fetch with SSRF-guarded manual redirects, timeout, and size cap.
    Returns (content_type, text)."""
    cfg = get_config()
    current = url
    async with httpx.AsyncClient(
        timeout=cfg.capture_timeout,
        follow_redirects=False,
        headers={"User-Agent": "cortex-capture/1.0"},
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            assert_public_url(current)
            r = await client.get(current)
            if r.status_code in (301, 302, 303, 307, 308) and "location" in r.headers:
                from urllib.parse import urljoin

                current = urljoin(current, r.headers["location"])
                continue
            r.raise_for_status()
            body = r.content[: cfg.capture_max_bytes + 1]
            if len(body) > cfg.capture_max_bytes:
                raise ValueError(f"response exceeds {cfg.capture_max_bytes} byte cap")
            return r.headers.get("content-type", ""), r.text
    raise ValueError("too many redirects")


def extract_readable(html: str) -> tuple[str, str]:
    """trafilatura readability: returns (title, plain_text)."""
    import trafilatura

    doc = trafilatura.extract(html, include_comments=False, include_tables=False) or html
    metadata = trafilatura.extract_metadata(html)
    title = (metadata.title if metadata else None) or "Untitled clipping"
    return title.strip(), doc.strip()


async def capture_url(
    conn: asyncpg.Connection,
    *,
    url: str,
    agent: str,
    harness: str | None = None,
    note: str | None = None,
    session: str | None = None,
    project: str | None = None,
) -> dict[str, Any]:
    cfg = get_config()
    async with conn.transaction():
        _ctype, html = await fetch_page(url)
        title, text = extract_readable(html)
        try:
            summary = await summarize_text(text[:12000]) if cfg.llm_enabled else None
        except Exception:
            log.exception("LLM summary failed, using heuristic summary")
            summary = None
        if not summary:
            summary = trunc(" ".join(text.split()), 400)

        body = f"> source: {url}\n> fetched: {datetime.now(timezone.utc).isoformat()}\n"
        if note:
            body += f"> note: {note}\n"
        body += f"\n# {title}\n\n## Summary\n\n{summary}\n\n## Extracted text\n\n{text[:20000]}"

        fetch_date = datetime.now(timezone.utc)
        ev = await events.append(
            conn,
            agent=agent,
            kind="research",
            harness=harness,
            session=session,
            project=project,
            payload={
                "title": f"Capture: {title}",
                "summary": summary,
                "source_url": url,
                "note": note,
                "tags": ["capture", "research"],
            },
        )
        nrow = await notes.create_note(
            conn,
            title=f"Capture: {title}",
            body=body,
            author=agent,
            tags=["capture", "research"],
            project=project,
            note_type="research",
            source_url=url,
            fetch_date=fetch_date,
            event_id=ev["id"],
        )
    return {
        "event_id": ev["id"],
        "note_id": str(nrow["id"]),
        "title": f"Capture: {title}",
        "url": url,
        "summary": summary,
        "fetch_date": fetch_date.isoformat(),
    }
