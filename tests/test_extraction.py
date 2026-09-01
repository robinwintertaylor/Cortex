"""Unit tests for the librarian's pure normalization functions (no LLM/DB)."""

from cortex.librarian.extraction import normalize, normalize_doc_links


def test_normalize_caps_and_strips_entities_facts_lessons_tags():
    raw = {
        "summary": "x" * 600,
        "entities": [{"name": f"e{i}", "type": "tool"} for i in range(10)],
        "facts": [{"subject": "s", "predicate": "p", "object": "o"} for _ in range(10)],
        "lesson_candidates": [f"lesson {i}" for i in range(5)],
        "tags": [f"tag {i}" for i in range(10)],
    }
    out = normalize(raw)
    assert len(out["summary"]) == 500
    assert len(out["entities"]) == 5
    assert len(out["facts"]) == 6
    assert len(out["lesson_candidates"]) == 3
    assert len(out["tags"]) == 6
    assert out["tags"][0] == "tag-0"  # lowercased, spaces -> hyphens


def test_normalize_drops_incomplete_facts():
    raw = {"facts": [{"subject": "s", "predicate": "", "object": "o"},
                     {"subject": "s", "predicate": "p", "object": "o"}]}
    out = normalize(raw)
    assert len(out["facts"]) == 1


def test_normalize_doc_links_keeps_only_exact_candidate_matches():
    candidates = ["cortex", "Postgres", "dsh"]
    raw = {"relates_to": ["Cortex", "made-up-entity", "postgres"]}
    picked = normalize_doc_links(raw, candidates)
    # case-insensitive match, but returns the candidate's original casing
    assert picked == ["cortex", "Postgres"]


def test_normalize_doc_links_dedupes_and_caps_at_eight():
    candidates = [f"e{i}" for i in range(10)]
    raw = {"relates_to": [f"e{i}" for i in range(10)] + ["e0"]}
    picked = normalize_doc_links(raw, candidates)
    assert len(picked) == 8
    assert len(set(picked)) == 8


def test_normalize_doc_links_empty_relates_to_is_valid():
    assert normalize_doc_links({}, ["cortex"]) == []
    assert normalize_doc_links({"relates_to": []}, ["cortex"]) == []
