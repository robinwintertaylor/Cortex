# Shared brain protocol (the Cortex constitution)

> Copy this text (or a pointer to it) into every harness's instruction file:
> `~/.claude/CLAUDE.md` · `~/.vibe/AGENTS.md` · `~/.config/goose/.goosehints` ·
> `~/.dsh/AGENTS.md` · Claude Desktop system prompt · Cursor
> `.cursor/rules/cortex.mdc` (see `deploy/cursor/`) · VS Code workspace
> `AGENTS.md` (see `deploy/vscode/`). Tool names are identical
> on every surface (MCP tools, REST endpoints, `brain` CLI) — NFR-8.

1. **AT SESSION START (before planning):** call `brain_context` for the active
   project and `brain_recent(limit 20)`. Acknowledge what other agents already
   did — do not redo or contradict their work. Also call `brain_declare_tools`
   with the tools and MCP servers you can reach — plain names are fine
   (`["ripgrep", "Docker"]`), or objects with `kind` (`mcp_server`, `app`,
   `cli`, `service`), `server` and `version` when you know them. The
   declaration replaces your previous one, so report your *current* toolset.
   This is what lets facts about tools point at a real entity instead of a bare
   string — and it tells other agents what you can actually do.
2. **BEFORE non-trivial choices:** run `brain_search("<topic>")` — do not
   re-decide decided things; if a decision exists and is active, follow it or
   write a superseding decision first (with rationale).
3. **AS YOU WORK:** capture useful external research with `brain_capture_url`.
4. **AT SESSION END (or before finishing):** `brain_log_action` with outcome +
   files; if you learned a durable lesson, `brain_lesson`; if you made a
   decision, `brain_log_decision`.
5. **Check the queue:** if a task is claimable and matches your mission, claim
   it with `brain_queue_claim` and complete it with `brain_queue_complete`.
6. **Never invent facts about other agents' actions** — read them from
   `brain_recent` / `brain_digest` and cite event ids.
7. Your identity is enforced by the server from your key. You cannot write as
   another agent, and neither can they write as you.
8. **When you produce a project document worth preserving as a real artifact**
   (a spec, plan, report, or other file — not just a log entry): upload the
   actual file with `brain_upload_file`, don't just describe it in a
   `brain_note`. Base64-encode the file and POST to
   `$CORTEX_URL/v1/brain_upload_file` with header
   `Authorization: Bearer $CORTEX_KEY` and body
   `{"filename": ..., "content_base64": ..., "project": ..., "tags": [...]}`.
   This stores it byte-identical, extracts its text for search, and makes it
   downloadable via `GET /v1/brain_file/{note_id}` — a text summary is not a
   substitute for the file itself.
