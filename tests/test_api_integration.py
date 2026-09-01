"""Integration tests against a live Postgres + API.

Requires a scratch database; skipped unless CORTEX_TEST_URL is set:

    docker compose up -d db
    CORTEX_TEST_URL=http://localhost:8738 pytest tests/test_api_integration.py

Covers the R1 exit criterion: two harnesses share one decision end-to-end
(goose logs a decision; claude-code's digest shows it).
"""

import os

import pytest

httpx = pytest.importorskip("httpx")

BASE = os.environ.get("CORTEX_TEST_URL", "")
pytestmark = pytest.mark.skipif(
    not BASE or not os.environ.get("CORTEX_TEST_ADMIN_KEY"),
    reason="set CORTEX_TEST_URL + CORTEX_TEST_ADMIN_KEY to run integration tests",
)


def _client(key: str) -> httpx.Client:
    return httpx.Client(base_url=BASE, headers={"Authorization": f"Bearer {key}"}, timeout=15)


@pytest.fixture(scope="module")
def keys():
    admin = _client(os.environ["CORTEX_TEST_ADMIN_KEY"])
    out = {}
    for aid in ("it-goose", "it-claude"):
        r = admin.post("/v1/admin/agents", json={"id": aid, "name": aid, "harness": aid,
                                                "role": "agent"})
        if r.status_code == 200:
            out[aid] = r.json()["key"]
        else:  # created in a previous run — revoke+recreate is overkill for CI
            out[aid] = None
    return out


def _key_or_skip(keys, aid):
    if not keys.get(aid):
        pytest.skip(f"{aid} key not creatable (already exists from a previous run)")


def test_healthz():
    with _client("whatever") as c:
        r = c.get("/healthz")
        assert r.status_code == 200 and r.json()["ok"] is True


def test_identity_is_enforced(keys):
    """FR-1 AC1: a write claiming agent=B stores agent=A."""
    _key_or_skip(keys, "it-goose")
    key = keys["it-goose"]
    with _client(key) as c:
        r = c.post("/v1/brain_log_action", json={"summary": "id test",
                                                 "agent": "someone-else"})
        assert r.status_code == 200
        evs = c.get("/v1/brain_recent", params={"agent": "someone-else", "limit": 5}).json()
        assert evs["events"] == []  # nothing was stored as someone-else
        mine = c.get("/v1/brain_recent", params={"agent": "it-goose", "limit": 5}).json()
        assert any(e["payload"].get("summary") == "id test" for e in mine["events"])


def test_revoked_key_gets_401(keys):
    """FR-1 AC2: revoked key returns 401 within 1s."""
    admin = _client(os.environ["CORTEX_TEST_ADMIN_KEY"])
    r = admin.post("/v1/admin/agents", json={"id": "it-rev", "name": "rev", "harness": "t"})
    assert r.status_code == 200
    key = r.json()["key"]
    with _client(key) as c:
        assert c.get("/v1/brain_recent").status_code == 200
    admin.delete("/v1/admin/agents/it-rev/key")
    with _client(key) as c:
        import time

        t0 = time.monotonic()
        r = c.get("/v1/brain_recent")
        assert r.status_code == 401 and time.monotonic() - t0 < 1.0


def test_shared_decision_end_to_end(keys):
    """R1 exit: A logs a decision; B's digest shows it (PRD §11)."""
    _key_or_skip(keys, "it-goose")
    _key_or_skip(keys, "it-claude")
    with _client(keys["it-goose"]) as c:
        r = c.post("/v1/brain_log_decision", json={
            "title": "IT: use qdrant", "options": ["qdrant", "pgvector"],
            "choice": "qdrant", "rationale": "test run", "project": "it-cortex"})
        assert r.status_code == 200, r.text
        decision = r.json()
        assert decision["status"] == "active" and decision["adr"].startswith("D-")
    with _client(keys["it-claude"]) as c:
        d = c.get("/v1/brain_digest?since=1h").json()
        assert any(decision["adr"] == x["adr"] for x in d["new_decisions"])
        s = c.get("/v1/brain_search", params={"q": "vector database choice", "limit": 5}).json()
        assert any(r["type"] == "decision" for r in s["results"])
        ctx = c.get("/v1/brain_context?project=it-cortex").json()
        assert len(ctx["active_decisions"]) >= 1


def test_idempotent_event_append(keys):
    """FR-2 AC2: identical idempotency key stores once."""
    _key_or_skip(keys, "it-goose")
    idem = {"summary": "idem", "idempotency_key": "it-idem-1"}
    with _client(keys["it-goose"]) as c:
        r1 = c.post("/v1/brain_log_action", json=idem)
        r2 = c.post("/v1/brain_log_action", json=idem)
        assert r1.json()["event_id"] == r2.json()["event_id"]


def test_hyphenated_agent_id_round_trip():
    """Regression (fixed e574fc7): ids like claude-code must authenticate.
    The old string-parse of the key split the id at the first hyphen and
    401'd every request for hyphenated agents."""
    admin = _client(os.environ["CORTEX_TEST_ADMIN_KEY"])
    r = admin.post("/v1/admin/agents", json={"id": "it-claude-code", "name": "it-cc",
                                             "harness": "claude-code"})
    if r.status_code != 200:
        pytest.skip("it-claude-code exists from a previous run")
    key = r.json()["key"]
    with _client(key) as c:
        r = c.post("/v1/brain_log_action", json={"summary": "hyphen id test"})
        assert r.status_code == 200, r.text
        evs = c.get("/v1/brain_recent", params={"agent": "it-claude-code",
                                                "limit": 5}).json()
        assert any(e["payload"].get("summary") == "hyphen id test"
                   for e in evs["events"])
    admin.delete("/v1/admin/agents/it-claude-code/key")


def test_hook_sink_accepts_documented_header():
    """Regression (fixed 6a3ac0f): /hook/* must accept X-Cortex-Hook-Token."""
    admin = _client(os.environ["CORTEX_TEST_ADMIN_KEY"])
    admin.post("/v1/admin/agents", json={"id": "it-hooks", "name": "it-hooks",
                                         "harness": "claude-code"})
    token = os.environ.get("CORTEX_HOOK_TOKEN", "")
    if not token:
        pytest.skip("CORTEX_HOOK_TOKEN not set")
    with httpx.Client(base_url=BASE, timeout=10) as raw:
        r = raw.post("/hook/tool-use?agent=it-hooks",
                     headers={"X-Cortex-Hook-Token": token},
                     json={"session_id": "it-s1", "hook_event_name": "PostToolUse",
                           "tool_name": "Write", "tool_input": {"path": "/tmp/x"}})
        assert r.status_code == 200, r.text
        r = raw.post("/hook/tool-use?agent=it-hooks",
                     headers={"X-Hook-Token": token},  # wrong name must fail
                     json={"session_id": "it-s1", "hook_event_name": "PostToolUse",
                           "tool_name": "Write", "tool_input": {"path": "/tmp/x"}})
        assert r.status_code == 401
    admin.delete("/v1/admin/agents/it-hooks/key")


def test_brain_lesson_with_verified_by(keys):
    """Regression: notes.add_lesson used a bare `$2 IS NOT NULL` inside a
    CASE expression alongside a typed VALUES-clause use of the same
    placeholder ($2 = verified_by). Postgres can't infer a type for that
    from context and 500'd with AmbiguousParameterError on every call that
    passed verified_by, at prepare time -- before any value even mattered."""
    _key_or_skip(keys, "it-goose")
    with _client(keys["it-goose"]) as c:
        r = c.post("/v1/brain_lesson", json={
            "statement": "integration test lesson", "verified_by": "it-goose"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "active"
