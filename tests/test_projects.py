"""Project-name canonicalisation (cortex/projects.py).

The event log is append-only, so historical rows keep whatever spelling they
were written with. canonical() fixes the write side; aliases_of() is what lets
a read still find those old rows.
"""

import json

import pytest

from cortex.projects import aliases_of, canonical


def test_known_aliases_canonicalise():
    assert canonical("second-brain") == "cortex"
    assert canonical("cortex-setup") == "cortex"
    assert canonical("it-cortex") == "cortex"
    assert canonical("bletchley-broadcast") == "Bletchley Broadcast"


def test_canonical_is_case_and_whitespace_insensitive():
    assert canonical("  Second-Brain  ") == "cortex"
    assert canonical("BLETCHLEY-BROADCAST") == "Bletchley Broadcast"


def test_unknown_and_empty_pass_through():
    assert canonical("some-new-project") == "some-new-project"
    assert canonical("cortex") == "cortex"
    assert canonical(None) is None
    assert canonical("") == ""


def test_canonical_is_idempotent():
    once = canonical("second-brain")
    assert canonical(once) == once


def test_aliases_of_expands_to_every_stored_spelling():
    got = aliases_of("cortex")
    assert set(got) == {"cortex", "cortex-setup", "second-brain", "it-cortex"}


def test_aliases_of_works_from_an_alias_not_just_the_canonical_name():
    # a filter typed as the old label must still find the whole group
    assert set(aliases_of("second-brain")) == set(aliases_of("cortex"))


def test_aliases_of_unknown_project_is_just_itself():
    assert aliases_of("brand-new") == ["brand-new"]
    assert aliases_of(None) == []


def test_env_override_replaces_defaults(monkeypatch):
    monkeypatch.setenv("CORTEX_PROJECT_ALIASES", json.dumps({"foo": "Bar"}))
    assert canonical("foo") == "Bar"
    # defaults no longer apply once overridden
    assert canonical("second-brain") == "second-brain"


@pytest.mark.parametrize("raw", ["not json", "[]", '"a string"'])
def test_malformed_env_override_falls_back_to_defaults(monkeypatch, raw):
    monkeypatch.setenv("CORTEX_PROJECT_ALIASES", raw)
    assert canonical("second-brain") == "cortex"
