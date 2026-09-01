"""Agent identity & keys (FR-1).

- Keys look like:  cx-<agent_id>-<secret>   (e.g. cx-goose-x7Fk...)
- Only the argon2id hash is stored; plaintext is never persisted or logged.
- Identity is resolved SERVER-SIDE from the bearer key; any `agent` field in a
  request body is advisory metadata and is overridden (AC1).
- Revoked keys fail within the auth check (AC2, 401).
"""

from __future__ import annotations

import ipaddress
import secrets
from contextvars import ContextVar
from datetime import datetime, timezone

import asyncpg
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from .config import get_config

_ph = PasswordHasher()  # argon2id with library defaults

# identity of the current request, set by API middleware / MCP middleware
current_agent: ContextVar[asyncpg.Record | None] = ContextVar("current_agent", default=None)

KEY_PREFIX = "cx-"


def hash_secret(secret: str) -> str:
    return _ph.hash(secret)


def verify_secret(secret: str, key_hash: str) -> bool:
    try:
        return _ph.verify(key_hash, secret)
    except VerifyMismatchError:
        return False


def generate_key(agent_id: str) -> str:
    """Return plaintext key. Caller prints it ONCE; only the hash is stored."""
    secret = secrets.token_urlsafe(24)
    return f"{KEY_PREFIX}{agent_id}-{secret}"


def key_agent_id(key: str) -> str | None:
    """Extract the agent id from a cx-<agent_id>-<secret> key."""
    if not key.startswith(KEY_PREFIX):
        return None
    body = key[len(KEY_PREFIX):]
    agent_id, sep, _ = body.partition("-")
    return agent_id if sep else None


async def create_agent(
    conn: asyncpg.Connection,
    agent_id: str,
    name: str,
    harness: str | None,
    role: str = "agent",
) -> tuple[str, str]:
    """Insert agent + key. Returns (agent_id, plaintext_key). Idempotent on id."""
    existing = await conn.fetchrow("SELECT id FROM agents WHERE id = $1", agent_id)
    if existing:
        raise ValueError(f"agent {agent_id!r} already exists")
    key = generate_key(agent_id)
    await conn.execute(
        """INSERT INTO agents (id, name, harness, role, key_hash)
           VALUES ($1, $2, $3, $4, $5)""",
        agent_id, name, harness, role, hash_secret(key),
    )
    return agent_id, key


async def revoke_agent_key(conn: asyncpg.Connection, agent_id: str) -> bool:
    row = await conn.execute(
        "UPDATE agents SET revoked = TRUE WHERE id = $1 AND NOT revoked", agent_id
    )
    return row.endswith("1")


async def resolve_bearer(conn: asyncpg.Connection, key: str) -> asyncpg.Record | None:
    """Resolve the caller identity from a bearer key. Returns the agent row or None.

    AC1/AC2/AC3: identity comes only from the key; revoked keys are rejected;
    the plaintext key never reaches storage or logs.
    """
    agent_id = key_agent_id(key)
    if not agent_id:
        return None
    row = await conn.fetchrow("SELECT * FROM agents WHERE id = $1", agent_id)
    if row is None or row["revoked"]:
        return None
    if not verify_secret(key, row["key_hash"]):
        return None
    await conn.execute(
        "UPDATE agents SET last_seen = now() WHERE id = $1", agent_id
    )
    return row


def is_admin(authorization: str | None) -> bool:
    """Bootstrap admin key from env (hmac-compare)."""
    cfg = get_config()
    if not cfg.admin_key:
        return False
    import hmac as _hmac

    if not authorization or not authorization.lower().startswith("bearer "):
        return False
    return _hmac.compare_digest(authorization[7:].strip(), cfg.admin_key)


def is_private_ip(host: str) -> bool:
    """True for loopback/private/link-local/unique-local/reserved ranges.

    Used by the SSRF guard (FR-11 AC1) and the hook-sink LAN check (NFR-4)."""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def is_lan_allowed(peer_host: str, extra_cidrs: list[str]) -> bool:
    if is_private_ip(peer_host):
        return True
    for cidr in extra_cidrs:
        try:
            if ipaddress.ip_address(peer_host) in ipaddress.ip_network(cidr):
                return True
        except ValueError:
            continue
    return False


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
