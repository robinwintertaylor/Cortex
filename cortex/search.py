"""Hybrid search (FR-6): Postgres FTS (BM25) + pgvector ANN + 1-hop fact-graph
expansion → RRF fusion (k=60) → recency prior → optional cross-encoder rerank
(P1, off by default; enable per-call with mode='rerank'). Filters are applied
pre-search. Exact identifier queries (e.g. 'D-004') are boosted to the top."""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any

import asyncpg

from . import metrics, notes as notes_mod
from .embeddings import embed_one, rerank_scores
from .events import fts_search as events_fts
from .facts import fact_to_dict
from .util import payload_dict

RRF_K = 60  # standard k for reciprocal-rank fusion
RECENCY_TAU_DAYS = 30.0
MAX_POOL = 50  # top-50 per leg, per FR-6


def _recency_prior(ts) -> float:
    if not ts:
        return 1.0
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return 1.0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0
    return 1.0 + math.exp(-age_days / RECENCY_TAU_DAYS)


def rrf_fuse(ranked_lists: list[list[tuple[str, dict[str, Any]]]], k: int = RRF_K) -> list[tuple[str, dict[str, Any], float]]:
    """Fuse ranked lists of (key, payload). Returns (key, payload, rrf_score)
    sorted desc. Pure function — unit tested."""
    scores: dict[str, float] = {}
    payload: dict[str, dict[str, Any]] = {}
    for lst in ranked_lists:
        for rank, (key, meta) in enumerate(lst, start=1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            if key not in payload:
                payload[key] = meta
    fused = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return [(key, payload[key], score) for key, score in fused]


def _event_result(r: asyncpg.Record) -> dict[str, Any]:
    payload = payload_dict(r["payload"])
    kind = r["kind"]
    if kind == "decision":
        disp_id = payload.get("adr") or f"D-{r['id']}"
    elif kind == "lesson":
        disp_id = f"L-{r['id']}"
    else:
        disp_id = f"E-{r['id']}"
    title = payload.get("title") or payload.get("statement") or payload.get("summary") or f"{kind} by {r['agent']}"
    snippet = payload.get("rationale") or payload.get("outcome") or payload.get("note") or str(payload)[:280]
    return {
        "id": disp_id,
        "type": kind,
        "title": str(title)[:200],
        "snippet": str(snippet)[:280],
        "provenance": {"agent": r["agent"], "harness": r["harness"], "ts": _iso(r["ts"])},
        "status": payload.get("status", "active"),
        "project": r["project"],
        "tags": payload.get("tags", []),
        "_raw": str(payload)[:2000],
        "_ts": _iso(r["ts"]),
    }


def _note_result(r: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": str(r["id"]),
        "type": r["note_type"],
        "title": r["title"],
        "snippet": r["body"][:280],
        "provenance": {"agent": r["author"], "harness": None, "ts": _iso(r["ts"])},
        "status": "active",
        "project": r["project"],
        "tags": list(r["tags"] or []),
        "url": r["source_url"],
        "_raw": f"{r['title']} {r['body']}"[:2000],
        "_ts": _iso(r["ts"]),
    }


def _fact_result(r: asyncpg.Record) -> dict[str, Any]:
    d = fact_to_dict(r)
    d["type"] = "fact"
    d["title"] = f"{d['subject']} {d['predicate']} {d['object']}"
    d["snippet"] = d["rationale"] or d["title"]
    d["provenance"] = {"agent": None, "harness": None, "ts": d["valid_from"]}
    d["_raw"] = d["title"][:2000]
    d["_ts"] = d["valid_from"]
    return d


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else None


async def _graph_expansion(
    conn: asyncpg.Connection, query: str, *, project: str | None
) -> list[tuple[str, dict[str, Any]]]:
    """1-hop fact-graph expansion: match query terms to entities, pull current
    facts touching them, and the events that produced those facts."""
    terms = [t for t in re.findall(r"[\w-]{3,}", query)][:6]
    if not terms:
        return []
    like = " OR ".join(f"lower(name) LIKE lower({'$' + str(i + 1)})" for i in range(len(terms)))
    entities = await conn.fetch(f"SELECT id, name FROM entities WHERE {like} LIMIT 20", *[f"%{t}%" for t in terms])
    if not entities:
        return []
    ids = [e["id"] for e in entities]
    rows = await conn.fetch(
        """
        SELECT f.* FROM facts f
        WHERE (f.subj = ANY($1::uuid[]) OR f.obj = ANY($1::uuid[]))
          AND f.valid_to IS NULL
        ORDER BY f.confidence DESC LIMIT 20
        """,
        ids,
    )
    out: list[tuple[str, dict[str, Any]]] = []
    for r in rows:
        res = _fact_result(r)
        out.append((res["id"], res))
        # the producing event is strong context too
        if r["episode_id"]:
            ev = await conn.fetchrow("SELECT * FROM events WHERE id = $1", r["episode_id"])
            if ev:
                eres = _event_result(ev)
                out.append((eres["id"], eres))
    return out


async def brain_search(
    conn: asyncpg.Connection,
    query: str,
    *,
    mode: str = "hybrid",
    type: str | None = None,
    agent: str | None = None,
    project: str | None = None,
    tag: str | None = None,
    since: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """The FR-6 query path. mode: hybrid | keyword | semantic | rerank."""
    limit = max(1, min(int(limit), 50))
    lists: list[list[tuple[str, dict[str, Any]]]] = []

    kind_filter = type if type in ("action", "decision", "lesson", "research", "note", "question") else None

    # keyword leg (events + notes)
    if mode in ("hybrid", "keyword", "rerank"):
        ev_rows = await events_fts(conn, query, limit=MAX_POOL, agent=agent,
                                    kind=kind_filter, project=project, since=since)
        lists.append([(f"E-{r['id']}", _event_result(r)) for r, _s in ev_rows])
        if not kind_filter:
            note_rows = await notes_mod.fts_search(conn, query, limit=MAX_POOL,
                                                   project=project, tag=tag)
            lists.append([(str(r["id"]), _note_result(r)) for r, _s in note_rows])

    # semantic leg (events + notes ANN)
    if mode in ("hybrid", "semantic", "rerank"):
        qvec = await embed_one(query)
        if any(qvec):
            ann_ev = await conn.fetch(
                """
                SELECT *, 1 - (embedding <=> $1) AS sim FROM events
                WHERE embedding IS NOT NULL AND 1 - (embedding <=> $1) > 0.25
                ORDER BY embedding <=> $1 LIMIT $2
                """,
                qvec, MAX_POOL,
            )
            lists.append([(f"E-{r['id']}", _event_result(r)) for r in ann_ev])
            ann_notes = await notes_mod.ann_search(conn, qvec, limit=MAX_POOL, project=project)
            lists.append([(str(r["id"]), _note_result(r)) for r, _s in ann_notes])

    # graph leg
    if mode in ("hybrid", "rerank"):
        lists.append(await _graph_expansion(conn, query, project=project))

    if not lists:
        return []

    fused = rrf_fuse(lists)

    # post-fusion: recency prior (per FR-6) + exact-identifier boost
    scored: list[tuple[float, dict[str, Any]]] = []
    exact = query.strip()
    for key, res, rrf_score in fused:
        prior = _recency_prior(res.get("_ts"))
        score = rrf_score * prior
        if res["id"].lower() == exact.lower() or res["title"].lower() == exact.lower():
            score += 10.0  # exact identifier match → top-1 (FR-6 AC2)
        scored.append((score, res))

    if mode == "rerank":
        top = [x for x in scored][:MAX_POOL]
        texts = [r["_raw"] for _s, r in top]
        rr = await rerank_scores(query, texts)
        scored = [(float(rr[i]) + 0.001 * s, r) for i, (s, r) in enumerate(top)]

    scored.sort(key=lambda x: x[0], reverse=True)
    results = []
    for score, res in scored[:limit]:
        res.pop("_raw", None)
        res.pop("_ts", None)
        res["score"] = round(float(score), 4)
        results.append(res)
    return results


async def timed_search(conn, *args, **kwargs) -> list[dict[str, Any]]:
    with metrics.search_latency.time():
        return await brain_search(conn, *args, **kwargs)
