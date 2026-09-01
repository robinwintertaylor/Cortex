# Cursor wiring

Extends the brain to another harness surface: Cursor's agent (chat, Composer,
background/terminal agents) speaks MCP, so it gets the full `brain_*` toolset.
No hooks system → logging is constitution-prompted (the model calls the tools,
like Claude Desktop), not auto-captured.

## 1. Register an agent + key

```bash
# from the repo root on the Cortex host — one command, prints the key AND
# the ready-to-paste mcp.json:
scripts/wire-harness.sh cursor Cursor
```

(or the manual equivalent: `docker compose run --rm cortex cortex admin
create-agent cursor --name "Cursor" --harness cursor` — key shown ONCE, store
it in your password manager)

## 2. MCP server

Global (all projects) — `~/.cursor/mcp.json`, or per-project `.cursor/mcp.json`
in the repo root:

```jsonc
{
  "mcpServers": {
    "cortex": {
      "url": "http://localhost:8738/mcp",
      "headers": { "Authorization": "Bearer cx-cursor-<secret>" }
    }
  }
}
```

- Restart Cursor after editing; tools appear as `brain_search`, `brain_context`, …
- `localhost` works because Cursor runs on the same machine as Cortex; from
  another machine use the LAN/tailnet address (`http://<device>:8738/mcp`).
- **Never commit a project-level `.cursor/mcp.json` containing a key** — add it
  to `.gitignore`.

## 3. Constitution — project rules

Copy `cortex.mdc` (this folder) into each project as
`.cursor/rules/cortex.mdc` (always-applied rule), or paste its body into your
global Cursor rules. It instructs the agent to bootstrap with
`brain_context`, search before deciding, and log actions/decisions/lessons at
the end of a task.

## Done when

- `brain_search` round-trip works in a Cursor chat/composer session
- A Cursor decision shows up in another harness's digest (`brain digest`)
  with `agent=cursor` provenance
