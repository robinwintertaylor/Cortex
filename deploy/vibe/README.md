# Mistral Vibe wiring (PRD §10)

## 1. Register an agent + key

```bash
cortex admin create-agent vibe --name "Mistral Vibe" --harness vibe
# → cx-vibe-<secret>
```

## 2. MCP server

```bash
export CORTEX_KEY=cx-vibe-<secret>
vibe mcp add cortex --url http://cortex.local:8738/mcp \
  --transport streamable-http --api-key-env CORTEX_KEY
```

(equivalently `[[mcp_servers]]` in `~/.vibe/config.toml`; tools appear as
`mcp_cortex_brain_*`)

## 3. Auto-capture hooks

Vibe's `post_tool` / `post_agent` hooks fire for subagents too — genuine
auto-write. Copy `hooks.toml` into the project (or `~/.vibe/hooks.toml`):

```toml
[[hooks]]
type = "post_tool"
command = "curl -sf -X POST http://cortex.local:8738/hook/tool-use?agent=vibe -H 'X-Cortex-Hook-Token: $CORTEX_HOOK_TOKEN' -H 'Content-Type: application/json' --data-binary @- >/dev/null || true"
```

The hook JSON (tool_input/tool_output/transcript_path) arrives on stdin and is
converted to an action event with agent=vibe (US-6).

## 4. Constitution

`~/.vibe/AGENTS.md`:

```markdown
See deploy/constitution.md — follow the shared brain protocol. Use the
mcp_cortex_brain_* tools.
```

## Done when

Post-tool events appear with agent=vibe; search + interactive logging
round-trip works in a chat.
