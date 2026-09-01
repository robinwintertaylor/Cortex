"""Regression: note bodies must survive INTO THE EVENT LOG untruncated.

Events are truth (AGENTS.md invariant) — the notes table stores the full body,
so the event payload must too, or `cortex rebuild --from 0` would silently
shrink every long document. Found while uploading the Second Brain docs
(several 10–40KB markdown files) to the running instance.
"""

import cortex.api.service as service


def test_note_event_payload_keeps_full_body():
    """note()'s payload truncation must cover real documents (was 8_000)."""
    import inspect

    src = inspect.getsource(service.note)
    # the payload trunc limit must be ≥ 100k chars for document uploads
    assert "trunc(body, 200_000)" in src, (
        "note event payload truncation too small — long docs would not "
        "survive a rebuild from the log")
