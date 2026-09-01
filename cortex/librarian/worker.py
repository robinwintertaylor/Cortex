"""The Librarian (FR-5): async worker over new events.

Pipeline per event: embed raw text (local ONNX) → deterministic projection of
decision/lesson events (rebuild-safe without an LLM) → optional LLM extraction
(disabled until configured) → consolidation (ADD/NOOP/SUPERSEDE/ADJUDICATE) →
upsert. Idempotent per event id (librarian_state) and fully replayable:
`cortex rebuild --from 0` reproduces the same facts (AC3) because every
projection is a pure function of the log.

Run with: python -m cortex.librarian.worker
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone

import asyncpg

from .. import metrics, notes as notes_mod, queue as queue_mod
from ..config import get_config
from ..db import get_pool, migrate
from ..embeddings import embed
from ..util import payload_dict
from ..facts import (
    add_fact,
    bump_confidence,
    current_facts_for_pred,
    get_or_create_entity,
    supersede,
)
from ..log import get_logger
from . import consolidation, extraction

log = get_logger(__name__)


def _payload(event) -> dict:
    return payload_dict(event["payload"])


async def _embed_event_text(conn, event) -> None:
    text = extraction.event_text(event)
    (vec,) = await embed([text])
    await conn.execute("UPDATE events SET embedding = $1 WHERE id = $2", vec, event["id"])


async def _project_tool_use(conn, event, payload) -> None:
    """Deterministic projection: hook/auto events carrying tool_name →
    fact(agent, 'uses_tool', tool). This is what populates the graph view
    with skills/MCPs/apps in use — no LLM required, rebuild-safe (AC3)."""
    tool = str(payload.get("tool_name") or "").strip()
    if not tool:
        return
    agent = str(event["agent"]).strip()
    if not agent:
        return
    existing = await current_facts_for_pred(conn, agent, "uses_tool")
    for e in existing:
        if consolidation.objects_equal(e["obj_text"], tool):
            await bump_confidence(conn, e["id"])
            return
    await add_fact(conn, subj_name=agent, pred="uses_tool", obj_text=tool,
                   episode_id=event["id"], kind="observed", confidence=0.7)


async def _project_decision(conn, event, payload) -> None:
    """Deterministic projection: decision event → fact(subj, 'decided', choice).
    Runs with or without an LLM so rebuilds are exact (AC3)."""
    choice = str(payload.get("choice") or "").strip()
    title = str(payload.get("title") or "").strip()
    if not choice:
        return
    subject = str(payload.get("project") or title or "the project")
    rationale = str(payload.get("rationale") or "")
    existing = await current_facts_for_pred(conn, subject, "decided")
    new = consolidation.NewFact(
        subj_name=subject, pred="decided", obj_text=choice,
        event_kind="decision", agent_role="owner",
    )
    action = consolidation.classify(new, [
        consolidation.ExistingFact(e["id"], e["subj_name"], e["pred"], e["obj_text"], e["kind"])
        for e in existing
    ])
    if action is consolidation.Action.NOOP:
        for e in existing:
            if consolidation.objects_equal(e["obj_text"], choice):
                await bump_confidence(conn, e["id"])
    else:  # ADD or SUPERSEDE-with-authority (decisions are authoritative)
        if existing and action is consolidation.Action.SUPERSEDE:
            old = next(e for e in existing if not consolidation.objects_equal(e["obj_text"], choice))
            await supersede(conn, old["id"], new_value=choice,
                            rationale=rationale or title, episode_id=event["id"],
                            kind="decided", confidence=0.95)
        else:
            await add_fact(conn, subj_name=subject, pred="decided", obj_text=choice,
                           episode_id=event["id"], kind="decided", confidence=0.95,
                           rationale=rationale or title)


async def _project_lesson(conn, event, payload) -> None:
    statement = str(payload.get("statement") or "").strip()
    if not statement:
        return
    row = await conn.fetchrow("SELECT id FROM lessons WHERE statement = $1", statement)
    if row:
        await conn.execute(
            "UPDATE lessons SET last_verified = now(), "
            "confidence = least(confidence + 0.05, 1.0) WHERE id = $1", row["id"],
        )
    else:
        await notes_mod.add_lesson(
            conn, statement=statement, verified_by=payload.get("verified_by"),
            project=event["project"], event_id=event["id"], confidence=0.9,
        )


async def _consolidate_extraction(conn, event, agent_role: str) -> None:
    """LLM extraction + the consolidation decision per extracted fact."""
    ex = await extraction.extract(event)
    payload = _payload(event)

    # entities with summaries (kept light: entity summary = latest extraction summary)
    for ent in ex["entities"]:
        e = await get_or_create_entity(conn, ent["name"], ent["type"])
        if ex["summary"] and not e["summary"]:
            # embedding = NULL: name-only embeddings are thin signal for doc
            # linking (_embed_pending_entities) — re-embed with the summary
            # now that one exists, which also re-triggers _link_new_entities.
            await conn.execute(
                "UPDATE entities SET summary = $1, embedding = NULL WHERE id = $2",
                ex["summary"], e["id"])

    for f in ex["facts"]:
        existing = await current_facts_for_pred(conn, f["subject"], f["predicate"])
        new = consolidation.NewFact(
            subj_name=f["subject"], pred=f["predicate"], obj_text=f["object"],
            event_kind=event["kind"], agent_role=agent_role,
        )
        sup_today = await conn.fetchval(
            """
            SELECT count(*) FROM facts
            WHERE lower(subj_name) = lower($1) AND pred = $2
              AND valid_to IS NOT NULL AND valid_to > now() - interval '1 day'
            """,
            f["subject"], f["predicate"],
        ) or 0
        action = consolidation.classify(new, [
            consolidation.ExistingFact(e["id"], e["subj_name"], e["pred"], e["obj_text"], e["kind"])
            for e in existing
        ], supersessions_today=int(sup_today))
        if action is consolidation.Action.ADD:
            await add_fact(conn, subj_name=f["subject"], pred=f["predicate"],
                          obj_text=f["object"], episode_id=event["id"],
                          kind="extracted", confidence=0.8, rationale=ex["summary"] or None)
        elif action is consolidation.Action.NOOP:
            for e in existing:
                if consolidation.objects_equal(e["obj_text"], f["object"]):
                    await bump_confidence(conn, e["id"])
        elif action is consolidation.Action.SUPERSEDE:
            old = next(e for e in existing if not consolidation.objects_equal(e["obj_text"], f["object"]))
            await supersede(conn, old["id"], new_value=f["object"],
                           rationale=ex["summary"], episode_id=event["id"], kind="extracted")
        else:  # ADJUDICATE — never silently overwrite (AC4)
            old = existing[0]
            await queue_mod.add(
                conn,
                kind="adjudication",
                title=f"Contradiction: {f['subject']} {f['predicate']} — "
                      f"'{f['object']}' vs '{old['obj_text']}'",
                created_by=event["agent"],
                project=event["project"],
                detail={
                    "new": {"subject": f["subject"], "predicate": f["predicate"],
                            "object": f["object"], "event_id": event["id"]},
                    "existing": {"fact_id": str(old["id"]), "object": old["obj_text"],
                                 "kind": old["kind"]},
                },
                fact_id=str(old["id"]),
            )

    for statement in ex["lesson_candidates"]:
        row = await conn.fetchrow("SELECT id FROM lessons WHERE statement = $1", statement)
        if not row:
            await notes_mod.add_lesson(conn, statement=statement, project=event["project"],
                                      event_id=event["id"], confidence=0.7)


async def process_event(conn: asyncpg.Connection, event, agent_role: str = "agent") -> None:
    """One event through the pipeline. Must run inside a transaction."""
    cfg = get_config()
    payload = _payload(event)

    # 1. embed raw event text (local, always on — vector search over events)
    try:
        await _embed_event_text(conn, event)
    except Exception:
        log.exception("embed failed for event %s", event["id"])

    # 2. deterministic projections (rebuildable without any LLM)
    if event["kind"] == "decision":
        await _project_decision(conn, event, payload)
    elif event["kind"] == "lesson":
        await _project_lesson(conn, event, payload)
    elif event["kind"] == "action" and payload.get("tool_name"):
        # hook/auto-captured tool use → graph edges (agent uses_tool tool)
        await _project_tool_use(conn, event, payload)

    # 3. optional LLM extraction (NFR-3: disabled until configured)
    if cfg.llm_enabled:
        try:
            await _consolidate_extraction(conn, event, agent_role)
        except Exception:
            log.exception("LLM extraction failed for event %s", event["id"],
                          extra={"event_id": event["id"]})


async def _upsert_doc_link(conn, note_id, entity_id, score: float, method: str = "embedding") -> None:
    # a doc can get the same (note, entity) pair from both passes below;
    # keep whichever signal is more confident rather than letting the
    # later writer blindly clobber the earlier one.
    await conn.execute(
        """
        INSERT INTO doc_links (note_id, entity_id, score, method)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (note_id, entity_id) DO UPDATE SET
            score = GREATEST(doc_links.score, EXCLUDED.score),
            method = CASE WHEN EXCLUDED.score > doc_links.score
                          THEN EXCLUDED.method ELSE doc_links.method END
        """,
        note_id, entity_id, score, method,
    )


async def _embed_pending_entities(conn) -> list[asyncpg.Record]:
    """Embed entities that lack one yet (mirrors the notes-embedding step
    below). Returns the newly-embedded rows so the caller can react to
    genuinely new entities instead of rescanning every doc every cycle."""
    rows = await conn.fetch(
        "SELECT id, name, etype, summary FROM entities WHERE embedding IS NULL LIMIT 25"
    )
    if not rows:
        return []
    vecs = await embed([f"{r['name']} ({r['etype'] or 'other'}) {r['summary'] or ''}".strip()
                        for r in rows])
    for r, v in zip(rows, vecs):
        await conn.execute("UPDATE entities SET embedding = $1 WHERE id = $2", v, r["id"])
    return rows


async def _link_new_entities_to_docs(conn, new_entities) -> None:
    """A freshly-embedded entity may be exactly what an already-uploaded,
    still-orphaned doc was 'about' all along — sweep current notes for it.
    This is what makes disconnected docs pick up connections *over time* as
    the entity graph grows, not just at upload time (graph.py's doc-linking
    docstring)."""
    cfg = get_config()
    for ent in new_entities:
        vec = await conn.fetchval("SELECT embedding FROM entities WHERE id = $1", ent["id"])
        if vec is None:
            continue
        matches = await conn.fetch(
            """
            SELECT id, 1 - (embedding <=> $1) AS score FROM notes
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> $1 LIMIT $2
            """,
            vec, cfg.doc_link_top_k,
        )
        for m in matches:
            if m["score"] < cfg.doc_link_min_score:
                continue
            await _upsert_doc_link(conn, m["id"], ent["id"], float(m["score"]))


async def _link_pending_notes_to_entities(conn) -> None:
    """A newly-embedded doc gets matched against the current entity set.

    NOT EXISTS doc_links means a note with zero qualifying matches gets
    reconsidered every cycle until one lands — fine at this system's scale
    (a personal/small-team second brain, LIMIT 25/cycle), and it's what lets
    a doc uploaded before any relevant entity existed still connect once one
    shows up, without a separate "already tried" column to maintain."""
    cfg = get_config()
    rows = await conn.fetch(
        """
        SELECT n.id, n.embedding FROM notes n
        WHERE n.embedding IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM doc_links dl WHERE dl.note_id = n.id)
        LIMIT 25
        """
    )
    for n in rows:
        matches = await conn.fetch(
            """
            SELECT id, 1 - (embedding <=> $1) AS score FROM entities
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> $1 LIMIT $2
            """,
            n["embedding"], cfg.doc_link_top_k,
        )
        for m in matches:
            if m["score"] < cfg.doc_link_min_score:
                continue
            await _upsert_doc_link(conn, n["id"], m["id"], float(m["score"]))


# LLM-asserted (not a computed similarity) — a fixed confidence, distinct
# from doc_link_min_score which only gates the embedding pass above.
LLM_DOC_LINK_SCORE = 0.75


async def _llm_link_pending_notes(conn) -> None:
    """Optional, richer pass (only runs if an LLM is configured): asks the
    LLM which known entities a note is actually *about*, catching related
    docs the embedding pass misses (a doc can discuss an entity at length
    without being textually/semantically close to that entity's name +
    summary). One-shot per note via llm_linked_at — unlike the embedding
    pass, a real API call per note isn't cheap enough to retry forever."""
    cfg = get_config()
    rows = await conn.fetch(
        "SELECT id, title, body FROM notes WHERE llm_linked_at IS NULL LIMIT 5"
    )
    if not rows:
        return
    candidates = [r["name"] for r in await conn.fetch(
        "SELECT name FROM entities ORDER BY created DESC LIMIT 200"
    )]
    for n in rows:
        try:
            picks = await extraction.suggest_doc_links(n, candidates)
            for name in picks:
                entity_id = await conn.fetchval(
                    "SELECT id FROM entities WHERE lower(name) = lower($1)", name
                )
                if entity_id:
                    await _upsert_doc_link(conn, n["id"], entity_id, LLM_DOC_LINK_SCORE, "llm")
        except Exception:
            log.exception("LLM doc-linking failed for note %s", n["id"])
        finally:
            # marked regardless of success/failure/empty result — see
            # docstring: this is deliberately one-shot, not a retry loop.
            await conn.execute("UPDATE notes SET llm_linked_at = now() WHERE id = $1", n["id"])


async def _agent_role(conn, agent: str) -> str:
    return await conn.fetchval("SELECT role FROM agents WHERE id = $1", agent) or "agent"


async def _one_cycle(conn) -> int:
    """Process a batch of new events + embed pending notes. Returns batch size."""
    rows = await conn.fetch(
        """
        SELECT e.* FROM events e
        LEFT JOIN librarian_state s ON s.event_id = e.id
        WHERE s.event_id IS NULL
        ORDER BY e.id LIMIT $1
        """,
        get_config().librarian_batch,
    )
    for event in rows:
        try:
            async with conn.transaction():
                role = await _agent_role(conn, event["agent"])
                await process_event(conn, event, role)
                await conn.execute(
                    "INSERT INTO librarian_state (event_id, state) VALUES ($1, 'done')",
                    event["id"],
                )
            metrics.librarian_processed.inc()
        except Exception as e:
            metrics.librarian_errors.inc()
            log.exception("event %s failed", event["id"], extra={"event_id": event["id"], "err": str(e)})
            try:
                await conn.execute(
                    """
                    INSERT INTO librarian_state (event_id, state, error)
                    VALUES ($1, 'error', $2)
                    ON CONFLICT (event_id) DO UPDATE SET state='error', error=$2, ts=now()
                    """,
                    event["id"], str(e)[:1000],
                )
            except Exception:
                log.exception("failed to record librarian error state")
    # notes pending embedding (captures, notes from API)
    pending_notes = await conn.fetch(
        "SELECT id, title, body FROM notes WHERE embedding IS NULL LIMIT 25"
    )
    if pending_notes:
        vecs = await embed([f"{n['title']} {n['body']}"[:2000] for n in pending_notes])
        for n, v in zip(pending_notes, vecs):
            await conn.execute("UPDATE notes SET embedding = $1 WHERE id = $2", v, n["id"])

    # doc↔entity graph linking (FR-13 graph view): new entities sweep
    # existing docs, then any still-unlinked doc gets matched against the
    # current entity set — see graph.py's build_graph docstring.
    new_entities = await _embed_pending_entities(conn)
    if new_entities:
        await _link_new_entities_to_docs(conn, new_entities)
    await _link_pending_notes_to_entities(conn)
    if get_config().llm_enabled:
        await _llm_link_pending_notes(conn)

    return len(rows)


async def _update_lag(conn) -> None:
    oldest = await conn.fetchval(
        """
        SELECT min(e.ts) FROM events e
        LEFT JOIN librarian_state s ON s.event_id = e.id
        WHERE s.event_id IS NULL
        """
    )
    lag = (datetime.now(timezone.utc) - oldest).total_seconds() if oldest else 0.0
    metrics.librarian_lag.set(lag)
    open_q = await conn.fetchval("SELECT count(*) FROM queue WHERE status = 'open'")
    open_adj = await conn.fetchval(
        "SELECT count(*) FROM queue WHERE status = 'open' AND kind = 'adjudication'")
    metrics.queue_depth.labels("task").set(open_q - open_adj)
    metrics.queue_depth.labels("adjudication").set(open_adj)


async def rebuild(from_id: int = 0) -> None:
    """FR-5 AC3: rebuild from the log. Clears projections, replays events."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM facts")
            await conn.execute("DELETE FROM doc_links")  # ON DELETE CASCADE would also catch this
            await conn.execute("DELETE FROM entities")
            await conn.execute("DELETE FROM lessons")
            await conn.execute("DELETE FROM librarian_state")
            await conn.execute("DELETE FROM notes WHERE derived")
            await conn.execute("UPDATE notes SET embedding = NULL, llm_linked_at = NULL")
            await conn.execute("UPDATE events SET embedding = NULL")
            await conn.execute("DELETE FROM queue WHERE kind = 'adjudication' AND status = 'open'")
        total = await conn.fetchval("SELECT count(*) FROM events WHERE id >= $1", from_id)
        log.info("rebuild: replaying %s events from id %s", total, from_id)
        done = 0
        while True:
            n = await _one_cycle(conn)
            done += n
            if n == 0:
                break
            if total:
                print(f"\r  {done}/{total}", end="", file=sys.stderr)
        print(file=sys.stderr)
        log.info("rebuild complete: %s events replayed", done)


async def run() -> None:
    """Main worker loop."""
    cfg = get_config()
    await migrate()
    pool = await get_pool()
    log.info("librarian up (llm=%s, model=%s)", cfg.llm_enabled or "disabled", cfg.llm_model)
    while True:
        try:
            async with pool.acquire() as conn:
                n = await _one_cycle(conn)
                await _update_lag(conn)
            if n == 0:
                await asyncio.sleep(cfg.librarian_poll_seconds)
        except Exception:
            metrics.librarian_errors.inc()
            log.exception("librarian cycle failed")
            await asyncio.sleep(5)


def main() -> None:
    from ..log import setup_logging

    setup_logging("INFO")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
