"""Regression: note events must not duplicate their note rows in search.

A note POST creates BOTH an event (kind='note') and a notes row with the same
content. Search used to return both, doubling every note hit (became very
visible when the Second Brain docs were uploaded as notes). The events arms
now exclude kind='note'; the notes arms serve them (and type=note queries).
"""

import cortex.search as search


def test_search_excludes_note_events():
    src = open(search.__file__, encoding="utf-8").read()
    # events arms exclude note-kind events …
    assert 'kind_not="note"' in src, "keyword events arm must exclude note events"
    assert "kind <> 'note'" in src, "ANN events arm must exclude note events"
    # … and are gated so type=note skips them entirely
    assert 'kind_filter != "note"' in src, "events arms must be gated off for type=note"
    # notes arms now also serve explicit type=note queries
    assert 'kind_filter is None or kind_filter == "note"' in src, (
        "notes arms must run for unfiltered and type=note searches")


def test_events_fts_supports_kind_not():
    """events.fts_search must expose kind_not (used for the dedup)."""
    import inspect
    import cortex.events as events

    sig = inspect.signature(events.fts_search)
    assert "kind_not" in sig.parameters
    src = inspect.getsource(events.fts_search)
    assert "kind <>" in src
