# DeepSeek Harness (dsh) wiring (PRD §10)

Three integration paths — use any or all (D-001: REST is the floor, MCP is the
fast path).

## 1. Register an agent + key

```bash
cortex admin create-agent dsh --name "DeepSeek Harness" --harness dsh
# → cx-dsh-<secret>
```

## 2. MCP via the official plugin (cordis.yml)

```yaml
# cordis.yml — add to your profile's plugin stack
- id: mcp-cortex
  name: "@deepseek-ai/dsh-mcp-client"
  config:
    serverName: cortex
    transport: streamable-http
    url: http://cortex.local:8738/mcp
    headers:
      Authorization: "Bearer cx-dsh-<secret>"
```

Tools surface as `mcp__cortex__brain_*` (60 s SDK timeout; tools only — no
resources/prompts — matches the plugin contract).

## 3. `brain` shell wrapper (works without any plugin)

```bash
sudo cp scripts/brain /usr/local/bin/brain && sudo chmod +x /usr/local/bin/brain
export CORTEX_URL=http://cortex.local:8738
export CORTEX_KEY=cx-dsh-<secret>
brain context; brain search "vector database"; brain log-action "wired dsh" --outcome ok
```

The DSH bash tool + headless wrapper complete search/context/log-action via
bash (FR-8 AC).

## 4. Constitution + headless wrapper

`~/.dsh/AGENTS.md`:

```markdown
See deploy/constitution.md — follow the shared brain protocol.
The cortex tools are available as mcp__cortex__brain_* or via the `brain`
shell command (bash tool).
```

Headless job wrapper (logs outcome after each run — US-3):

```bash
#!/usr/bin/env bash
# dsh-nightly-job.sh
dsh --profile headless "$@"
rc=$?
brain log-action "nightly job: $*" --outcome $([ $rc -eq 0 ] && echo ok || echo "failed($rc)")
exit $rc
```

## Done when

Headless job outcome visible in the next digest (every other harness sees it).
