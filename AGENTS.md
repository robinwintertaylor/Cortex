# Cortex — notes for agent harnesses working in (or wiring into) this repo

You are reading this because an agent harness (DSH, Claude Code, Cursor, VS
Code Copilot, Goose, Vibe, …) has you working in this repository. This file is
your self-service onboarding. Human-facing docs: `README.md`.

## What this repo is

Cortex is a **self-hosted shared second brain**: an append-only event log plus
a temporal knowledge graph (entities + facts with provenance and supersession)
behind one API with three doors — MCP at `/mcp`, REST at `/v1/*`, and SSE at
`/v1/stream`. A **librarian** worker embeds every event locally (ONNX) and,
optionally, extracts entities/facts with a pluggable LLM. Design rule #1:
**events are truth, everything else is a rebuildable projection.**

## Wiring YOURSELF into the brain (self-service)

If your harness isn't wired up yet, follow these steps — no human needed
except for minting your key:

1. **Find the service.** Default `http://localhost:8738`. Check
   `~/.config/cortex/agent-keys.key` (mode 600) for existing credentials —
   one `agent-id=cx-<secret>` line per harness.
2. **Get an identity.** If your harness's id already has a line in that file,
   use that key. Otherwise ask the owner to run
   `scripts/wire-harness.sh <your-harness> "<Display Name>"` from the repo
   root (it mints the key and prints your exact config block), or the
   manual form: `docker compose run --rm cortex cortex admin create-agent
   <your-harness> --name "<You>" --harness <your-harness>`. Append the
   printed key to `~/.config/cortex/agent-keys.key`. Identity
   is enforced server-side from the key — a write claiming a different agent
   stores YOUR id, not theirs. Never put keys in committed files.
3. **Follow your recipe** in `deploy/<your-harness>/README.md` (MCP config,
   hooks, instruction files — each one is complete and current). If no recipe
   exists for your harness yet, use the nearest MCP-capable one
   (`deploy/cursor/` is the minimal template) and consider adding yours.
4. **Adopt the constitution** (`deploy/constitution.md`): bootstrap with
   `brain_context` at session start; `brain_search` before non-trivial
   choices (never re-decide an active decision — supersede it, with
   rationale); `brain_capture_url` for research; `brain_log_action` /
   `brain_log_decision` / `brain_lesson` before finishing; claim queue work;
   cite event ids, never invent other agents' actions.

## Tool surface (identical over MCP, REST, and the `brain` shell wrapper)

`brain_context` · `brain_search` · `brain_recent` · `brain_digest` ·
`brain_read` · `brain_graph` (knowledge-graph view) · `brain_entities` ·
`brain_facts_about` · `brain_supersede_fact` (owner only) · `brain_note` ·
`brain_log_action` · `brain_log_decision` · `brain_lesson` ·
`brain_capture_url` · `brain_queue_add/claim/complete` · `brain_agents`

REST mirrors every tool 1:1 (`POST /v1/brain_log_decision`,
`GET /v1/brain_digest?since=48h&fmt=md`, …) with `Authorization: Bearer
<key>`. The `brain` wrapper (`scripts/brain`, install to `~/.local/bin`)
resolves its key from the key file automatically.

## Rules when working ON this repo

- **Events are append-only.** No UPDATE/DELETE path on `events` may ever be
  added; the only sanctioned purge is the audited `scripts/purge_events.py`.
- **Rebuildability is a hard invariant**: every projection (facts, entities,
  lessons, notes-derived) must be a pure function of the log —
  `cortex rebuild --from 0` must reproduce the same state.
- **Never trust self-declared identity**: agent identity comes only from the
  verified bearer key (see `cortex/security.py::resolve_bearer` — note agent
  ids may contain hyphens; don't parse ids out of key strings).
- Keys/argon2id: plaintext agent keys are shown once at creation and never
  stored or logged by the service.
- MCP support is dual-era (SDK v1 `FastMCP` and v2 `MCPServer`) — keep
  `cortex/api/mcp.py` working with both.
- Run tests before committing: `pytest` (unit, no DB). Integration tests need
  a live stack: see `tests/test_api_integration.py` header.
