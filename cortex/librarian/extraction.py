"""Librarian extraction pass (FR-5): raw event → structured knowledge.

The LLM is asked for JSON-schema-constrained output. Output is validated and
normalized here; extraction quality is gated by the golden-set eval (AC2)."""

from __future__ import annotations

import json
from typing import Any

from ..llm import extract_json

SYSTEM_PROMPT = """You are the Cortex librarian: an extraction engine for an AI agents' shared brain.
You read one event from an append-only log and extract durable knowledge.

Return ONLY a JSON object with this exact shape:
{
  "summary": "one-sentence summary of the event",
  "entities": [{"name": "...", "type": "project|tech|person|concept|tool|other"}],
  "facts": [{"subject": "...", "predicate": "snake_case_predicate", "object": "...",
             "object_is_entity": false}],
  "lesson_candidates": ["durable, generally-useful lessons only"],
  "tags": ["lowercase", "single-word-tags"]
}

Rules:
- Facts are (subject, predicate, object) triples about stable things, e.g.
  {"subject": "cortex", "predicate": "uses_database", "object": "postgres 16"}.
- Predicates: snake_case, verb-like (uses, chose, runs_on, depends_on, decided, ...).
- NEVER invent facts not stated or directly implied by the event text.
- Ignore transient chatter; an empty extraction is a valid answer ({}).
- At most 5 entities, 6 facts, 3 lesson_candidates, 6 tags."""


def event_text(event) -> str:
    payload = event["payload"] if isinstance(event["payload"], dict) else json.loads(event["payload"] or "{}")
    return json.dumps(
        {
            "kind": event["kind"],
            "agent": event["agent"],
            "project": event["project"],
            "title": payload.get("title"),
            "summary": payload.get("summary"),
            "outcome": payload.get("outcome"),
            "choice": payload.get("choice"),
            "rationale": payload.get("rationale"),
            "statement": payload.get("statement"),
            "note": payload.get("note"),
            "options": payload.get("options"),
            "extra": {k: v for k, v in payload.items()
                      if k not in ("title", "summary", "outcome", "choice",
                                   "rationale", "statement", "note", "options")},
        },
        default=str,
    )


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate + clamp the LLM's JSON into a safe extraction result."""
    entities, seen = [], set()
    for e in (raw.get("entities") or [])[:5]:
        name = str(e.get("name", "")).strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        entities.append({"name": name[:120], "type": str(e.get("type", "other"))[:32]})

    facts = []
    for f in (raw.get("facts") or [])[:6]:
        subj = str(f.get("subject", "")).strip()
        pred = str(f.get("predicate", "")).strip()
        obj = str(f.get("object", "")).strip()
        if not (subj and pred and obj):
            continue
        facts.append({
            "subject": subj[:120],
            "predicate": pred[:64],
            "object": obj[:300],
            "object_is_entity": bool(f.get("object_is_entity")),
        })

    lessons = [str(s).strip()[:400] for s in (raw.get("lesson_candidates") or [])[:3] if str(s).strip()]
    tags = [str(t).strip().lower().replace(" ", "-")[:32] for t in (raw.get("tags") or [])[:6] if str(t).strip()]

    return {
        "summary": str(raw.get("summary", ""))[:500],
        "entities": entities,
        "facts": facts,
        "lesson_candidates": lessons,
        "tags": tags,
    }


async def extract(event) -> dict[str, Any]:
    """LLM extraction of one event. Raises if the LLM is not configured."""
    raw = await extract_json(SYSTEM_PROMPT, event_text(event))
    return normalize(raw)
