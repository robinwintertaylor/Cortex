#!/usr/bin/env bash
# wire-harness.sh — mint an agent key + print the exact config to paste.
# The ONE command the owner runs; the agent (or you) does the rest.
#
# Usage: scripts/wire-harness.sh <harness-id> [display-name]
#   harness-id: cursor | vscode | claude-code | claude-desktop | goose | vibe | dsh
#
# Example:
#   scripts/wire-harness.sh cursor Cursor
#   → creates agent, prints key + ready-to-paste ~/.cursor/mcp.json
set -euo pipefail

h="${1:?usage: wire-harness.sh <cursor|vscode|claude-code|claude-desktop|goose|vibe|dsh> [display-name]}"
name="${2:-$h}"

# run from repo root (compose context)
cd "$(dirname "$0")/.."

if ! command -v docker >/dev/null; then
  echo "docker not found — run this on the Cortex host" >&2; exit 1
fi

key=$(docker compose run --rm cortex cortex admin create-agent "$h" \
        --name "$name" --harness "$h" | grep -o 'cx-[a-zA-Z0-9_-]*' | head -1)
if [ -z "$key" ]; then
  echo "agent creation failed (already registered? re-run with a new id, or" \
       "check docker compose output above)" >&2; exit 1
fi

url="http://localhost:8738/mcp"
case "$h" in
  cursor)
    cat <<EOF
✓ agent 'cursor' created. Paste into ~/.cursor/mcp.json (global) or
  <project>/.cursor/mcp.json, then restart Cursor:

{
  "mcpServers": {
    "cortex": {
      "url": "$url",
      "headers": { "Authorization": "Bearer $key" }
    }
  }
}

Then copy deploy/cursor/cortex.mdc to .cursor/rules/cortex.mdc in each project.
EOF
    ;;
  vscode)
    cat <<EOF
✓ agent 'vscode' created. Paste into <project>/.vscode/mcp.json,
  then reload the window:

{
  "servers": {
    "cortex": {
      "type": "http",
      "url": "$url",
      "headers": { "Authorization": "Bearer $key" }
    }
  }
}

Then add the AGENTS.md protocol from deploy/vscode/README.md to the workspace root.
EOF
    ;;
  claude-code|claude-desktop|goose|vibe|dsh)
    cat <<EOF
✓ agent '$h' created with key: $key

Follow deploy/$h/README.md for the wiring steps (this harness has a richer
recipe: hooks/auto-capture in addition to MCP).
EOF
    ;;
  *)
    cat <<EOF
✓ agent '$h' created with key: $key

No dedicated recipe — use deploy/cursor/README.md as the minimal MCP template.
EOF
    ;;
esac

# offer to append to the local key file (what the brain wrapper reads)
kf="$HOME/.config/cortex/agent-keys.key"
if [ -t 0 ]; then
  printf "append to %s for the 'brain' wrapper? [y/N] " "$kf"
  read -r ans
  if [ "${ans:-n}" = "y" ]; then
    mkdir -p "$(dirname "$kf")"
    echo "$h=$key" >> "$kf" && chmod 600 "$kf"
    echo "✓ appended ($h=$key)"
  fi
fi
