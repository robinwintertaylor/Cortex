"""Entity type backfill (cortex/librarian/extraction.py).

A quarter of this brain's entities came out of extraction with no type, so they
all rendered as the same muted slate and no type filter could reach them. The
LLM fills the gap, but its answer is only trusted through the same defensive
parsing as every other model output: a name it did not receive, or a type
outside the closed vocabulary, is discarded rather than stored.
"""

import pytest

from cortex.facts import ENTITY_TYPES
from cortex.librarian.extraction import normalize_entity_types

NAMES = ["claude-code", "Docker", "Bletchley Broadcast", "the project"]


def test_valid_picks_are_kept():
    raw = {"types": {"claude-code": "agent", "Docker": "tech"}}
    assert normalize_entity_types(raw, NAMES) == {"claude-code": "agent", "Docker": "tech"}


def test_name_matching_is_case_insensitive_but_returns_the_original():
    raw = {"types": {"DOCKER": "tech"}}
    assert normalize_entity_types(raw, NAMES) == {"Docker": "tech"}


def test_type_matching_is_case_insensitive():
    assert normalize_entity_types({"types": {"Docker": "TECH"}}, NAMES) == {"Docker": "tech"}


def test_invented_names_are_discarded():
    """The model is told to use only names from the list; when it invents one
    anyway, storing it would create an entity nobody asked for."""
    raw = {"types": {"Kubernetes": "tech", "Docker": "tech"}}
    assert normalize_entity_types(raw, NAMES) == {"Docker": "tech"}


def test_types_outside_the_vocabulary_are_discarded():
    raw = {"types": {"Docker": "database", "claude-code": "agent"}}
    assert normalize_entity_types(raw, NAMES) == {"claude-code": "agent"}


@pytest.mark.parametrize("etype", ENTITY_TYPES)
def test_every_vocabulary_member_survives(etype):
    assert normalize_entity_types({"types": {"Docker": etype}}, NAMES) == {"Docker": etype}


def test_doc_is_not_a_storable_entity_type():
    """graph.py renders a 'doc' group, but docs are notes — never entity rows,
    so the typer must not be able to write that value."""
    assert "doc" not in ENTITY_TYPES
    assert normalize_entity_types({"types": {"Docker": "doc"}}, NAMES) == {}


@pytest.mark.parametrize("raw", [{}, {"types": None}, {"types": []}, {"types": "nope"},
                                 {"wrong_key": {"Docker": "tech"}}])
def test_malformed_responses_yield_nothing(raw):
    assert normalize_entity_types(raw, NAMES) == {}


def test_whitespace_in_the_reply_is_tolerated():
    assert normalize_entity_types({"types": {"  Docker  ": " tech "}}, NAMES) == {"Docker": "tech"}


def test_empty_candidate_list_accepts_nothing():
    assert normalize_entity_types({"types": {"Docker": "tech"}}, []) == {}


def test_reply_size_is_capped():
    names = [f"e{i}" for i in range(200)]
    raw = {"types": {n: "other" for n in names}}
    assert len(normalize_entity_types(raw, names)) == 100
