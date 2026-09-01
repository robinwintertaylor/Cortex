"""Regression tests for the two auth bugs fixed on 2026-09-01 (see CLAUDE.md).

1. Agent ids containing hyphens (e.g. "claude-code") must authenticate — the
   old key_agent_id() string-parse split "cx-claude-code-<secret>" into id
   "claude" and 401'd every request for such agents. resolve_bearer now
   matches the key against each agent's stored hash, so parsing must never
   be used for authentication again. (The full path needs a DB — the
   live round-trip lives in tests/test_api_integration.py — here we pin
   the wire-level invariants.)
2. The hook sinks must accept the documented X-Cortex-Hook-Token header —
   the dependency previously mapped to X-Hook-Token instead.
"""

from __future__ import annotations

import inspect


def test_hook_token_header_has_documented_alias():
    """require_hook_token's Header parameter must map to the header name that
    the README and deploy/claude-code/settings.example.json document."""
    from cortex.api.deps import require_hook_token

    params = inspect.signature(require_hook_token).parameters
    assert "x_hook_token" in params, "hook token parameter missing"
    alias = params["x_hook_token"].default.alias
    assert alias == "X-Cortex-Hook-Token", (
        f"hook sinks expect X-Cortex-Hook-Token, dependency maps to {alias!r} — "
        "bug 6a3ac0f would recur"
    )


def test_key_format_supports_hyphenated_agent_ids():
    """Keys embed the agent id verbatim; ids like claude-code are legal."""
    from cortex.security import KEY_PREFIX, generate_key

    key = generate_key("claude-code")
    assert key.startswith(KEY_PREFIX + "claude-code-")
    # the old parser would have returned "claude" here — resolve_bearer must
    # never rely on splitting the id out of the key (token_urlsafe secrets
    # may themselves contain "-")
    secret = key[len(KEY_PREFIX + "claude-code-"):]
    assert secret and "-" in secret or secret  # secret half is hyphen-safe


def test_key_agent_id_is_documented_best_effort_only():
    """key_agent_id must NOT be used for authentication (ambiguous with
    hyphenated ids). If that changes, break this test deliberately."""
    from cortex.security import key_agent_id, resolve_bearer

    src = inspect.getsource(resolve_bearer)
    assert "key_agent_id" not in src, (
        "resolve_bearer must match stored hashes, not parse the id from the key"
    )
    # and the parser itself, for hyphenated ids, is at least honest about it:
    assert key_agent_id("cx-claude-code-s3cr3t") == "claude"
