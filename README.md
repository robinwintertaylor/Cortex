# Cortex — a shared second brain for multiple agent harnesses

Self-hosted memory service that lets Claude Code, Claude Desktop, DeepSeek
Harness (dsh), Mistral Vibe, Goose, Cursor, VS Code (Copilot), scripts/cron —
and you — share one brain.

**Design tenets** (from `plans/prd-cortex.md`):

> events are truth, indexes are cache · identity on every write · extraction
> is a separate pass · hybrid retrieval everywhere · local-first, no cloud
> lock-in

- **Append-only event log** is the source of truth; every other structure
  (facts, notes, lessons, entities) is a projection rebuildable via replay.
- **Bi-temporal knowledge graph**: entities + facts with validity windows and
  provenance; contradictions become supersession chains, never overwrites.
- **Per-agent identity enforced server-side** from bearer keys (agents can
  never write "as someone else").
- **Hybrid retrieval**: Postgres FTS (BM25) + pgvector ANN + 1-hop graph
  expansion → RRF fusion (k=60) → recency prior → optional cross-encoder
  rerank. Raw events are searchable the moment they're appended.
- **Local-first**: embeddings are local ONNX (fastembed/bge-m3); LLM extraction
  is pluggable (DeepSeek API or ollama) and *disabled until configured* —
  append/search/digest all work without it. Zero data leaves the LAN by
  default.

## Quickstart

```bash
cd Cortex
cp .env.example .env
# set CORTEX_ADMIN_KEY + CORTEX_HOOK_TOKEN (e.g. output of: cortex admin genkey)
docker compose up -d --build

# register your first agents (keys shown once):
pip install -e .
cortex admin create-agent claude-code --name "Claude Code" --harness claude-code
cortex admin create-agent goose --name "Goose" --harness goose
cortex admin create-agent dsh --name "DSH" --harness dsh --role owner
```

- API: `http://localhost:8738` — `/v1/*` REST, `/mcp` (streamable-HTTP MCP),
  `/v1/stream` SSE, `/hook/*` sinks, `/healthz`, `/metrics`
- Dashboard: `http://localhost:8740` — timeline, hybrid search, decision browser, **knowledge-graph view** (`/graph`, vis-network vendored locally: entities as nodes, facts as edges, supersession dashed, click a node for its facts), queue/digest, agents
- Librarian runs as its own compose service (embeds + extracts + consolidates)

## Layout

```
cortex/
  config.py, db.py, schema.py        config, pool, DDL (idempotent migrate)
  security.py                        argon2id keys, server-side identity, SSRF helpers
  events.py, facts.py, notes.py, queue.py   projections (log, graph, notes, queue)
  search.py                          hybrid search + RRF fusion + rerank
  digest.py, capture.py, export.py   digest/context, URL capture, markdown export
  embeddings.py, llm.py              local ONNX embeddings, pluggable LLM
  librarian/                         async worker: extract → consolidate → embed
  api/                               FastAPI app: REST /v1, MCP /mcp, SSE, hooks
  dashboard.py, cli.py               read-only web UI, admin/export/rebuild CLI
scripts/                             brain wrapper, MCP stdio bridge, backup, purge
deploy/                             per-harness wiring for all six harnesses
tests/                               unit (consolidation, RRF, SSRF, keys) + integration
```

## API examples (PRD §9 shapes)

```bash
KEY=cx-goose-...
curl -s -X POST http://localhost:8738/v1/brain_log_decision \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"title":"Use Qdrant for Cortex vector index","options":["qdrant","pgvector"],
       "choice":"qdrant","rationale":"hybrid filters + ops simplicity","project":"cortex"}'
# → {"event_id":12,"fact_id":"…","adr":"D-12","status":"active"}

curl -s -H "Authorization: Bearer $KEY" \
  'http://localhost:8738/v1/brain_search?q=what+did+we+decide+about+the+vector+database&limit=5'

curl -s -H "Authorization: Bearer $KEY" 'http://localhost:8738/v1/brain_digest?since=48h'
```

Every MCP tool mirrors these 1:1 (`brain_context`, `brain_search`, `brain_recent`,
`brain_digest`, `brain_read`, `brain_note`, `brain_log_action`,
`brain_log_decision`, `brain_lesson`, `brain_capture_url`, `brain_entities`,
`brain_facts_about`, `brain_supersede_fact`, `brain_queue_add/claim/complete`,
`brain_agents`). Tool names are stable across Brainstem/Cortex/Hive (NFR-8).

## Wiring each harness

| Harness | Recipe |
|---|---|
| Claude Code | `deploy/claude-code/` — MCP add + SessionStart digest hook + PostToolUse/SessionEnd HTTP hook sinks |
| Claude Desktop | `deploy/claude-desktop/` — stdio bridge (`scripts/cortex-mcp-stdio`) |
| Mistral Vibe | `deploy/vibe/` — `vibe mcp add` + `hooks.toml` auto-capture |
| Goose | `deploy/goose/` — extension + Open Plugins `hooks.json` + MOIM feed |
| DeepSeek Harness | `deploy/dsh/` — cordis.yml `dsh-mcp-client` + `brain` wrapper + AGENTS.md |
| Scripts/cron | REST (`scripts/brain`, `cortex sweep`, `scripts/backup.sh`) |
| Cursor | `deploy/cursor/` — MCP server in `mcp.json` + always-applied project rule (constitution-prompted logging) |
| VS Code (Copilot) | `deploy/vscode/` — MCP server in `.vscode/mcp.json` + `AGENTS.md` protocol |
| Buzz (P2) | `deploy/buzz/` — one-way digest bridge only |

The shared constitution (session protocol every agent follows) is
`deploy/constitution.md`.

## Operations

- **Backups (FR-15):** nightly `scripts/backup.sh` (pg_dump + retention);
  WAL archiving documented for RPO ≤ 5 min; restore drill in the script header;
  `cortex rebuild --from 0` replays the log as final fallback.
- **Observability (FR-16):** structured JSON logs; Prometheus `/metrics`
  (append rate, librarian lag, search latency histogram, queue depth, SSE
  subscribers); alert when `cortex_librarian_lag_seconds` > 1800.
- **Purge:** the only sanctioned event purge is the audited
  `scripts/purge_events.py --confirm`.
- **LLM setup (optional):** point `CORTEX_LLM_BASE_URL` at any
  OpenAI-compatible endpoint (DeepSeek API, ollama). Without it the librarian
  still embeds events, projects decisions/lessons deterministically, and raw
  search works — extraction simply stays off.

## Tests

```bash
pip install -e '.[dev]'
pytest                       # unit tests (no DB needed)
CORTEX_TEST_URL=http://localhost:8738 \
CORTEX_TEST_ADMIN_KEY=cx-admin-... pytest tests/test_api_integration.py
```

## Release status vs PRD

Built per `plans/prd-cortex.md` v1.0 (R1+R2 core, R3 partially): FR-1..FR-16
implemented (FR-12/13 as lean P2 versions; rerank pluggable and off by
default per open-question #2). Golden-set extraction eval (FR-5 AC2) and the
1M-event load test (R3) are the remaining owner runbooks — see PRD §11.
