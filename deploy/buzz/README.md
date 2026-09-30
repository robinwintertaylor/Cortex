# Buzz wiring

Buzz is a Nostr-based messaging app that runs managed AI agents (e.g. Fizz,
Honey, Pollen) as Claude Code processes on the owner's machine. Two seams:

1. **Buzz agents as Cortex clients.** Each agent reads and writes the brain
   under its own identity.
2. **Digest bridge (optional).** A cron job posts the daily digest into a Buzz
   channel.

## 1. Buzz agents as Cortex clients

### Why a CLI, not MCP

Several Buzz agents share one machine and one Claude Code config, and Buzz
has no per-agent MCP header. A user-scope MCP entry would give every agent the
same key, and the brain couldn't tell them apart. `scripts/buzz-brain` fixes
that: it's a stdlib-only Python REST client that picks the key per agent.

- **Identity.** The agent id is `CORTEX_AGENT` if set. Otherwise it's
  `buzz-<slug of BUZZ_ACP_DISPLAY_NAME>`, an env var Buzz sets for each managed
  agent, so `Fizz` becomes `buzz-fizz`. The key is the matching
  `buzz-fizz=cx-…` line in `~/.config/cortex/agent-keys.key`. As everywhere
  else, the server enforces identity from the key: the display name only
  chooses which key to send.
- **Buzz context.** For writes that take a session (`brain_log_action`,
  `_log_decision`, `_lesson`, `_note`, `_capture_url`, `_upload_file`,
  `_declare_tools`), `--channel <uuid>` or `BUZZ_CHANNEL` stamps
  `session=buzz:<channel-uuid>`. Every event then traces back to the Buzz
  conversation that caused it. An explicit `"session"` in the JSON wins.

### Register each agent (owner, once per agent)

```bash
scripts/wire-harness.sh buzz-fizz "Fizz (Buzz)"     # harness=buzz; answer y to save the key
scripts/wire-harness.sh buzz-honey "Honey (Buzz)"
```

To record who is who, write a `brain_note` (tags `buzz`, `identity`) that maps
each `buzz-*` id to its Buzz display name and Nostr pubkey
(`buzz users get --name <Name>`).

### Install the wrapper

```bash
cp scripts/buzz-brain ~/.local/bin/buzz-brain && chmod +x ~/.local/bin/buzz-brain
# Windows (for PowerShell / cmd), next to it:
printf '@python "%%~dp0buzz-brain" %%*\r\n' > ~/.local/bin/buzz-brain.cmd
buzz-brain check        # → {"agent": "buzz-<you>", "registered": true, ...}
```

### Usage

```bash
buzz-brain whoami
buzz-brain brain_context '{"project": "cortex"}'
buzz-brain brain_recent '{"limit": 20}'
echo '{"query": "hook token"}' | buzz-brain brain_search -
buzz-brain brain_read '{"id": "E-10"}'
buzz-brain --channel "$CHANNEL_UUID" brain_log_action \
  '{"summary": "…", "outcome": "done", "files": ["…"], "project": "cortex"}'
```

Any `brain_*` tool works, and the JSON body matches the REST route.

### Constitution

Buzz agents read `AGENTS.md` in their workspace (`~/.buzz/AGENTS.md`). Put the
protocol **below** the `BUZZ MANAGED` markers, since Buzz regenerates the rest.
Point it at `deploy/constitution.md`, give it the `buzz-brain` commands above,
and tell agents to always pass `--channel` with the channel UUID from their
turn context.

### Done when

- `buzz-brain check` reports `registered: true` for each agent
- A write shows `agent=buzz-<name>`, `harness=buzz`,
  `session=buzz:<channel-uuid>` in `brain_read`
- Two agents' writes land under two different agent ids

## 2. Digest bridge (optional)

A cron job posts the daily digest into a Buzz channel so the owner sees it
where they already are:

```bash
# daily digest → Buzz channel (buzz CLI, BUZZ_PRIVATE_KEY + BUZZ_RELAY_URL set)
17 8 * * *  CORTEX_AGENT=buzz-fizz buzz-brain brain_digest '{"since": "24h", "fmt": "md"}' \
              | buzz messages send --channel "$BUZZ_CHANNEL" --content -
```

Every event is exportable (`/v1/brain_recent`, `cortex export --markdown`),
so Buzz messages could later be indexed back into the brain the same way.
