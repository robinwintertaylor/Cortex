# Visual Studio Code (GitHub Copilot) wiring

Eighth harness: VS Code's Copilot Chat / agent mode speaks MCP (VS Code 1.99+),
so the `brain_*` toolset is available in chat and agent sessions. No hooks →
logging is constitution-prompted via `AGENTS.md`/chat instructions (same model
as Claude Desktop).

## 1. Register an agent + key

```bash
cd ~/Projects/Cortex
docker compose run --rm cortex cortex admin create-agent vscode \
  --name "VS Code Copilot" --harness vscode
# → cx-vscode-<secret>   (shown ONCE — store it in your password manager)
```

## 2. MCP server

Workspace-level — `.vscode/mcp.json` in the repo root:

```jsonc
{
  "servers": {
    "cortex": {
      "type": "http",
      "url": "http://localhost:8738/mcp",
      "headers": { "Authorization": "Bearer cx-vscode-<secret>" }
    }
  }
}
```

- Or add it user-wide: Command Palette → **"MCP: Add Server…"** → HTTP →
  paste the URL, then store the key via the suggested
  `"inputs"` secret flow (VS Code keeps headers out of the file that way).
- Reload the window afterwards; enable the tools in Copilot Chat's tools
  picker (they appear under the `cortex` server).
- `localhost` works because VS Code runs on the same machine as Cortex; from
  another machine use the LAN/tailnet address.
- **Never commit a `.vscode/mcp.json` containing a key** — add it to
  `.gitignore` (`.vscode/mcp.json` is workspace-local config, not shareable).

## 3. Constitution

Add `AGENTS.md` to the workspace root with the protocol text below (Copilot
agent mode reads AGENTS.md):

```markdown
# Shared brain protocol (Cortex)
Use the cortex MCP tools in agent sessions:
1. At session start: brain_context (project) + brain_recent(limit 20).
2. Before non-trivial choices: brain_search("<topic>") — follow active
   decisions instead of re-deciding; supersede with rationale if needed.
3. Capture research with brain_capture_url.
4. At session end: brain_log_action(summary, outcome, files, project);
   brain_lesson for durable lessons; brain_log_decision for decisions.
5. brain_queue: claim (brain_queue_claim) and complete tasks in your mission.
6. Never invent facts about other agents — read brain_recent/brain_digest,
   cite event ids.
```

## Done when

- A `brain_search` round-trip works in Copilot Chat with the cortex tools enabled
- A VS Code decision appears in another harness's digest with `agent=vscode`
