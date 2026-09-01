"""Unit tests: since-parser, hook idempotency, digest rendering."""

from cortex.api.service import hook_idempotency_key
from cortex.digest import render_digest_md
from cortex.util import parse_since, slugify, trunc


def test_parse_since_relative():
    from datetime import datetime, timezone

    then = parse_since("48h")
    delta = datetime.now(timezone.utc) - then
    assert abs(delta.total_seconds() - 48 * 3600) < 120


def test_parse_since_iso():
    dt = parse_since("2025-06-01T00:00:00Z")
    assert dt.year == 2025 and dt.month == 6


def test_parse_since_epoch():
    assert parse_since("0").year == 1970


def test_parse_since_bad_raises():
    import pytest

    with pytest.raises(ValueError):
        parse_since("sometime")


def test_slugify():
    assert slugify("Use Qdrant for Cortex vector index!") == "use-qdrant-for-cortex-vector-index"
    assert slugify("!!!") == "untitled"


def test_trunc():
    assert trunc("abc", 2) == "a…"
    assert trunc("abc", 10) == "abc"


def test_payload_dict_normalizes_jsonb_strings():
    from cortex.util import payload_dict

    assert payload_dict({"title": "x"}) == {"title": "x"}
    assert payload_dict('{"title": "x"}') == {"title": "x"}
    assert payload_dict(None) == {}
    assert payload_dict("not-json") == {}
    assert payload_dict("[1, 2]") == {}


def test_hook_idempotency_deterministic():
    p = {"session_id": "s-1", "hook_event_uuid": "u-9", "hook_event_name": "PostToolUse"}
    k1 = hook_idempotency_key(p)
    k2 = hook_idempotency_key(p)
    assert k1 == k2 and k1.startswith("hook-")
    assert hook_idempotency_key({"session_id": "s-1"}) is None


def test_digest_md_renders_empty_and_full():
    md = render_digest_md({
        "since": "2025-01-01T00:00:00+00:00", "actions_by_agent": {},
        "new_decisions": [], "new_facts": [], "superseded_facts": [],
        "new_lessons": [], "open_questions": [], "open_queue": [],
    })
    assert "Nothing changed" in md

    md = render_digest_md({
        "since": "2025-01-01T00:00:00+00:00",
        "actions_by_agent": {"goose": [{"event_id": 42, "summary": "chose qdrant",
                                        "outcome": "done", "project": "cortex"}]},
        "new_decisions": [{"event_id": 41, "adr": "D-004",
                           "title": "Use Qdrant", "choice": "qdrant",
                           "agent": "goose", "project": "cortex"}],
        "new_facts": [{"subject": "cortex", "predicate": "decided", "object": "qdrant",
                       "kind": "decided", "confidence": 0.95, "rationale": None,
                       "valid_from": "x", "valid_to": None, "superseded_by": None,
                       "episode_id": None, "id": "f9"}],
        "superseded_facts": [], "new_lessons": [], "open_questions": [],
        "open_queue": [{"id": "q1", "kind": "task", "title": "t", "status": "open"}],
    })
    assert "D-004" in md and "E#42" in md and "goose" in md
    assert "cortex decided qdrant" in md
