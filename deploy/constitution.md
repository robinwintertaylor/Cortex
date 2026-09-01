# Shared brain protocol (the Cortex constitution)

> Copy this text (or a pointer to it) into every harness's instruction file:
> `~/.claude/CLAUDE.md` · `~/.vibe/AGENTS.md` · `~/.config/goose/.goosehints` ·
> `~/.dsh/AGENTS.md` · Claude Desktop system prompt. Tool names are identical
> on every surface (MCP tools, REST endpoints, `brain` CLI) — NFR-8.

1. **AT SESSION START (before planning):** call `brain_context` for the active
   project and `brain_recent(limit 20)`. Acknowledge what other agents already
   did — do not redo or contradict their work.
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
