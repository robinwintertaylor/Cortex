# Claude Code wiring (PRD §10)

## 1. Register an agent + key

```bash
cd Cortex
cortex admin create-agent claude-code --name "Claude Code" --harness claude-code
# → cx-claude-code-<secret>   (shown ONCE)
```

## 2. MCP server

```bash
claude mcp add --transport http cortex http://cortex.local:8738/mcp
# then set the header/auth per your claude version, or use env:
#   CORTEX_KEY=cx-claude-code-...
```

Claude Code speaks the stateless 2026-07-28 revision — no initialize needed;
the same `/mcp` endpoint serves all clients (legacy fallback included).

## 3. Hooks — auto-capture + session-start digest

Copy `settings.example.json` into `~/.claude/settings.json` (merge with
existing). It wires:

- **SessionStart** → `curl` the digest; stdout lands in the session context
  (unprompted digest, R1 exit criterion).
- **PostToolUse** (Write/Edit/Bash) → HTTP POST to `/hook/tool-use` — edits log
  as events (agent=claude-code) without any model effort.
- **SessionEnd** → HTTP POST to `/hook/session-end`.

The hooks carry the shared token from `CORTEX_HOOK_TOKEN` (env or inline in
the settings file — settings.json is local to your machine).

## 4. Constitution

Add to `~/.claude/CLAUDE.md` (or project `CLAUDE.md`):

```markdown
See deploy/constitution.md — follow the shared brain protocol.
Use the cortex MCP tools (brain_*) at session start, before decisions, and at
session end.
```

## Done when (PRD §10)

- Digest appears unprompted in a new session (SessionStart hook)
- Edits log without model effort (PostToolUse hook)
- Search + log round-trip over MCP works
