"""is_entity_like — the guard between a fact edge and a junk entity.

The extractor is asked for an `object_is_entity` flag and over-reports it,
happily marking whole clauses as entities. Every clause that gets through mints
an entity row that then anchors a meaningless region of the graph, so the flag
alone is not enough to link facts.obj.
"""

import pytest

from cortex.facts import is_entity_like

NAMES = [
    "HeyGen",
    "Docker",
    "PostgreSQL",
    "agent framework",
    "Tier 1 collection agent",
    "claude-code",
    "Bletchley Broadcast",
]

CLAUSES = [
    "cortex local deployment is verified",
    "the API was migrated to FastAPI",
    "agents 012-015 have been retired",
    "this should use pgvector",
    "the worker can process ten events",
    "extraction did not run",
]


@pytest.mark.parametrize("name", NAMES)
def test_real_names_are_entity_like(name):
    assert is_entity_like(name) is True


@pytest.mark.parametrize("clause", CLAUSES)
def test_clauses_are_rejected(clause):
    assert is_entity_like(clause) is False


def test_empty_and_none_rejected():
    assert is_entity_like(None) is False
    assert is_entity_like("") is False
    assert is_entity_like("   ") is False


def test_overlong_names_rejected():
    assert is_entity_like("x" * 61) is False
    assert is_entity_like("x" * 60) is True


def test_too_many_words_rejected():
    assert is_entity_like("one two three four five") is True
    assert is_entity_like("one two three four five six") is False


def test_sentence_punctuation_rejected():
    assert is_entity_like("Redis.") is False
    assert is_entity_like("Redis, Postgres") is False
    assert is_entity_like("really?") is False
    assert is_entity_like("note: a thing") is False


def test_internal_whitespace_is_normalised():
    assert is_entity_like("agent   framework") is True
