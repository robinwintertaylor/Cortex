# Goose wiring (PRD §10)

## 1. Register an agent + key

```bash
cortex admin create-agent goose --name "Goose" --harness goose
# → cx-goose-<secret>
```

Goose's `streamable_http` extensions accept `uri` only (OAuth pre-registered)
or use an env-passed header — for static keys the simplest reliable route on
current builds is a stdio bridge via our scripts/cortex-mcp-stdio:

```yaml
# ~/.config/goose/config.yaml
extensions:
  cortex:
    name: Cortex
    cmd: /usr/local/bin/cortex-mcp-stdio
    envs:
      CORTEX_URL: http://cortex.local:8738
      CORTEX_KEY: cx-goose-<secret>
    enabled: true
```

(If your build supports header auth on `streamable_http`, the direct
`{type: streamable_http, uri: http://cortex.local:8738/mcp}` form works too —
tools surface as `mcp_cortex_brain_*`.)

## 2. Auto-capture — Open Plugins hooks

```bash
mkdir -p ~/.agents/plugins/cortex/hooks
cp hooks.json ~/.agents/plugins/cortex/hooks/hooks.json
```

PostToolUse / AfterFileEdit / SessionEnd POST to the hook sinks → events with
agent=goose.

## 3. MOIM feed (P1)

Point `GOOSE_MOIM_MESSAGE_FILE` at a file the digest refreshes:

```bash
*/5 * * * * curl -sf -H "Authorization: Bearer $GOOSE_KEY" \
  "http://cortex.local:8738/v1/brain_digest?since=24h" | python3 -c \
  'import json,sys; print(json.dumps(sys.stdin.read())[:64000])' \
  > /tmp/goose-moim.json
export GOOSE_MOIM_MESSAGE_FILE=/tmp/goose-moim.json
```

## 4. Constitution

`~/.config/goose/.goosehints`:

```markdown
See deploy/constitution.md — follow the shared brain protocol.
```

## Done when

Hooks fire (events with agent=goose); MOIM shows the current digest every turn.
