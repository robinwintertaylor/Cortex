"""Tool registry normalisation (cortex/tools.py).

The registry only earns its keep if what goes in is clamped and deduplicated —
it exists to be a *closed* vocabulary the fact extractor can resolve against,
so a sloppy declaration would defeat the point.
"""

import pytest

from cortex.tools import DEFAULT_KIND, KINDS, normalize, tool_to_dict


class Row(dict):
    def __getitem__(self, k):
        return dict.__getitem__(self, k)


def test_bare_strings_become_records():
    assert normalize(["Docker", "ripgrep"]) == [
        {"name": "Docker", "kind": DEFAULT_KIND, "server": None, "version": None},
        {"name": "ripgrep", "kind": DEFAULT_KIND, "server": None, "version": None},
    ]


def test_objects_keep_their_detail():
    (t,) = normalize([{"name": "cortex", "kind": "mcp_server",
                       "server": "localhost:8738", "version": "1.0.0"}])
    assert t == {"name": "cortex", "kind": "mcp_server",
                 "server": "localhost:8738", "version": "1.0.0"}


def test_mixed_forms_are_accepted_together():
    out = normalize(["ripgrep", {"name": "Docker", "kind": "app"}])
    assert [t["name"] for t in out] == ["ripgrep", "Docker"]
    assert [t["kind"] for t in out] == [DEFAULT_KIND, "app"]


def test_duplicates_are_dropped_case_insensitively():
    out = normalize(["Docker", "docker", {"name": "DOCKER"}])
    assert len(out) == 1 and out[0]["name"] == "Docker"


def test_unknown_kind_falls_back_to_the_default():
    (t,) = normalize([{"name": "x", "kind": "wharrgarbl"}])
    assert t["kind"] == DEFAULT_KIND


@pytest.mark.parametrize("kind", KINDS)
def test_every_declared_kind_survives(kind):
    (t,) = normalize([{"name": "x", "kind": kind}])
    assert t["kind"] == kind


def test_kind_is_case_insensitive():
    (t,) = normalize([{"name": "x", "kind": "MCP_Server"}])
    assert t["kind"] == "mcp_server"


def test_blank_and_malformed_entries_are_skipped():
    assert normalize(["", "   ", None, 42, {"kind": "app"}, {}]) == []


def test_empty_input_is_safe():
    assert normalize([]) == []
    assert normalize(None) == []


def test_fields_are_clamped():
    (t,) = normalize([{"name": "n" * 300, "server": "s" * 300, "version": "v" * 100}])
    assert len(t["name"]) == 120
    assert len(t["server"]) == 120
    assert len(t["version"]) == 40


def test_declaration_size_is_capped():
    assert len(normalize([f"tool-{i}" for i in range(500)])) == 200


def test_whitespace_is_stripped():
    (t,) = normalize(["  Docker  "])
    assert t["name"] == "Docker"


def test_tool_to_dict_exposes_the_serving_shape():
    row = Row({"agent": "claude-code", "name": "Docker", "tool_kind": "app",
               "server": None, "version": "27", "entity_id": None,
               "first_declared": None, "last_declared": None})
    assert tool_to_dict(row) == {
        "agent": "claude-code", "name": "Docker", "kind": "app", "server": None,
        "version": "27", "entity_id": None,
        "first_declared": None, "last_declared": None,
    }
