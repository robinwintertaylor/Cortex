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
            await conn.execute("UPDATE entities SET summary = $1 WHERE id = $2",
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

    # 3. optional LLM extraction (NFR-3: disabled until configured)
    if cfg.llm_enabled:
        try:
            await _consolidate_extraction(conn, event, agent_role)
        except Exception:
            log.exception("LLM extraction failed for event %s", event["id"],
                          extra={"event_id": event["id"]})


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
            await conn.execute("DELETE FROM entities")
            await conn.execute("DELETE FROM lessons")
            await conn.execute("DELETE FROM librarian_state")
            await conn.execute("DELETE FROM notes WHERE derived")
            await conn.execute("UPDATE notes SET embedding = NULL")
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
