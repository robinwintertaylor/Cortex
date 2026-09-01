"""Cortex DDL — Postgres 16 + pgvector (FR-1/2/4, Plan 2 §9.1).

Invariants:
  * events is append-only — no UPDATE/DELETE path exists anywhere in the codebase
    (admin purge is a standalone audited script, scripts/purge_events.py).
  * everything outside events is a projection, rebuildable via `cortex rebuild --from 0`.
  * facts are never deleted, only superseded.
  * every artifact carries provenance (agent, harness, session, episode_id).

The embedding dimension is baked in at first migration from CORTEX_EMBED_DIM.
Statements are idempotent (IF NOT EXISTS) so re-running is safe.
"""

from __future__ import annotations

STATEMENTS_TEMPLATE = [
    "CREATE EXTENSION IF NOT EXISTS vector",
    "CREATE EXTENSION IF NOT EXISTS pgcrypto",
    # ── agents (FR-1) ────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS agents(
      id        TEXT PRIMARY KEY,
      name      TEXT NOT NULL,
      harness   TEXT,
      role      TEXT NOT NULL DEFAULT 'agent',
      key_hash  TEXT NOT NULL,
      revoked   BOOLEAN NOT NULL DEFAULT FALSE,
      last_seen TIMESTAMPTZ,
      created   TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # ── events: the append-only log (FR-2) ──────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS events(
      id             BIGSERIAL PRIMARY KEY,
      ts             TIMESTAMPTZ NOT NULL DEFAULT now(),
      agent          TEXT NOT NULL,
      harness        TEXT,
      session        TEXT,
      kind           TEXT NOT NULL,
      project        TEXT,
      payload        JSONB NOT NULL,
      idempotency_key TEXT UNIQUE,
      embedding      vector({dim}),
      payload_tsv    tsvector GENERATED ALWAYS AS
                      (to_tsvector('simple', payload::text)) STORED
    )
    """,
    "CREATE INDEX IF NOT EXISTS events_ts_idx ON events (ts DESC)",
    "CREATE INDEX IF NOT EXISTS events_agent_ts_idx ON events (agent, ts DESC)",
    "CREATE INDEX IF NOT EXISTS events_kind_ts_idx ON events (kind, ts DESC)",
    "CREATE INDEX IF NOT EXISTS events_project_ts_idx ON events (project, ts DESC)",
    "CREATE INDEX IF NOT EXISTS events_payload_tsv_idx ON events USING gin (payload_tsv)",
    "CREATE INDEX IF NOT EXISTS events_payload_gin_idx ON events USING gin (payload)",
    # ── entities (FR-4) ──────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS entities(
      id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      etype     TEXT,
      name      TEXT UNIQUE NOT NULL,
      summary   TEXT,
      embedding vector({dim}),
      created   TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS entities_embedding_idx ON entities USING hnsw (embedding vector_cosine_ops)",
    # ── facts: bi-temporal knowledge graph (FR-4) ───────────────────────────
    """
    CREATE TABLE IF NOT EXISTS facts(
      id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      subj         UUID REFERENCES entities(id),
      subj_name    TEXT NOT NULL,
      pred         TEXT NOT NULL,
      obj          UUID REFERENCES entities(id),
      obj_text     TEXT,
      valid_from   TIMESTAMPTZ NOT NULL DEFAULT now(),
      valid_to     TIMESTAMPTZ,
      superseded_by UUID REFERENCES facts(id),
      episode_id   BIGINT REFERENCES events(id),
      kind         TEXT NOT NULL DEFAULT 'extracted',  -- extracted|decided|lesson|owner
      rationale    TEXT,
      confidence   REAL NOT NULL DEFAULT 0.8,
      embedding    vector({dim})
    )
    """,
    "CREATE INDEX IF NOT EXISTS facts_subj_pred_idx ON facts (subj_name, pred)",
    "CREATE INDEX IF NOT EXISTS facts_valid_idx ON facts (valid_from DESC) WHERE valid_to IS NULL",
    # ── notes (incl. research captures FR-11) ────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS notes(
      id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      title      TEXT NOT NULL,
      body       TEXT NOT NULL,
      tags       TEXT[] NOT NULL DEFAULT '{}',
      project    TEXT,
      author     TEXT NOT NULL,
      note_type  TEXT NOT NULL DEFAULT 'note',     -- note|research|summary
      derived    BOOLEAN NOT NULL DEFAULT FALSE,   -- true = librarian-created
      embedding  vector({dim}),
      links      TEXT[] NOT NULL DEFAULT '{}',
      source_url TEXT,
      fetch_date TIMESTAMPTZ,
      event_id   BIGINT REFERENCES events(id),
      ts         TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS notes_embedding_idx ON notes USING hnsw (embedding vector_cosine_ops)",
    """
    CREATE INDEX IF NOT EXISTS notes_fts_idx ON notes USING gin
      (to_tsvector('simple', title || ' ' || body))
    """,
    "CREATE INDEX IF NOT EXISTS notes_tags_idx ON notes USING gin (tags)",
    # ── file uploads: notes rows with note_type='document' carry the extra
    # columns below; body holds the extracted text (searched/embedded like
    # any other note), storage_path points at the content-addressed blob on
    # disk for download. Added as ALTER TABLE so existing deployments pick
    # this up on next boot without a manual migration step.
    "ALTER TABLE notes ADD COLUMN IF NOT EXISTS mime_type TEXT",
    "ALTER TABLE notes ADD COLUMN IF NOT EXISTS size_bytes BIGINT",
    "ALTER TABLE notes ADD COLUMN IF NOT EXISTS sha256 TEXT",
    "ALTER TABLE notes ADD COLUMN IF NOT EXISTS storage_path TEXT",
    "ALTER TABLE notes ADD COLUMN IF NOT EXISTS original_filename TEXT",
    "CREATE INDEX IF NOT EXISTS notes_sha256_idx ON notes (sha256) WHERE sha256 IS NOT NULL",
    # ── doc_links: embedding-discovered doc↔entity connections ───────────────
    # Docs (notes) aren't entities and carry no fact rows, so graph.py's
    # project/tags/links string-match is the only edge signal for a doc
    # *unless* the librarian has found a semantic match here. Populated by
    # librarian.worker's linking pass (never by hand); ON DELETE CASCADE so a
    # `cortex rebuild` (which wipes entities) or a note delete drops stale
    # links for free instead of orphaning rows or tripping an FK error.
    """
    CREATE TABLE IF NOT EXISTS doc_links(
      note_id   UUID NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
      entity_id UUID NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
      score     REAL NOT NULL,
      method    TEXT NOT NULL DEFAULT 'embedding',
      created   TIMESTAMPTZ NOT NULL DEFAULT now(),
      PRIMARY KEY (note_id, entity_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS doc_links_entity_idx ON doc_links (entity_id)",
    # ── lessons (FR-4/FR-5 outputs) ─────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS lessons(
      id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      statement    TEXT NOT NULL,
      verified_by  TEXT,
      status       TEXT NOT NULL DEFAULT 'active',  -- active|decayed|retired
      project      TEXT,
      event_id     BIGINT REFERENCES events(id),
      confidence   REAL NOT NULL DEFAULT 0.8,
      embedding    vector({dim}),
      last_verified TIMESTAMPTZ,
      ts           TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # ── shared queue (FR-12) + adjudication board (FR-5) ─────────────────────
    """
    CREATE TABLE IF NOT EXISTS queue(
      id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      kind       TEXT NOT NULL DEFAULT 'task',      -- task|adjudication
      title      TEXT NOT NULL,
      detail     JSONB,
      status     TEXT NOT NULL DEFAULT 'open',     -- open|claimed|done|adjudicated
      project    TEXT,
      created_by TEXT,
      claimed_by TEXT,
      claimed_at TIMESTAMPTZ,
      fact_id    UUID REFERENCES facts(id),
      created    TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS queue_status_idx ON queue (status, created DESC)",
    # ── librarian bookkeeping — keeps events append-only (FR-5 idempotency) ─
    """
    CREATE TABLE IF NOT EXISTS librarian_state(
      event_id BIGINT PRIMARY KEY REFERENCES events(id),
      state    TEXT NOT NULL,                       -- done|error
      error    TEXT,
      ts       TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # ── notify trigger for SSE (FR-9) ────────────────────────────────────────
    """
    CREATE OR REPLACE FUNCTION cortex_notify_event() RETURNS trigger AS $$
      BEGIN
        PERFORM pg_notify('cortex_events', json_build_object(
          'id', NEW.id, 'agent', NEW.agent, 'kind', NEW.kind,
          'project', NEW.project, 'ts', NEW.ts
        )::text);
        RETURN NEW;
      END
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS cortex_events_notify ON events",
    "CREATE TRIGGER cortex_events_notify AFTER INSERT ON events "
    "FOR EACH ROW EXECUTE FUNCTION cortex_notify_event()",
]


def ddl_statements(dim: int) -> list[str]:
    # str.replace, not str.format: SQL uses '{}' for empty arrays and
    # json_build_object keys that .format() would treat as placeholders.
    return [s.replace("{dim}", str(dim)) for s in STATEMENTS_TEMPLATE]
