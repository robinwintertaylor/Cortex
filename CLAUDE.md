# Cortex — notes for agents working in this repo

Read `AGENTS.md` first — it is the self-service onboarding (what the brain
is, how to wire a harness in, tool surface, hard invariants). This file adds
Claude-Code-specific notes.

## Bugs fixed 2026-09-01

- **`cortex/security.py` — `resolve_bearer` mis-parsed agent ids containing
  hyphens.** `key_agent_id()` split `cx-<agent_id>-<secret>` on the first
  hyphen, so an id like `claude-code` (the README's own example) resolved to
  `claude` and every request 401'd, even with a freshly issued valid key.
  Fixed by matching the key against each registered agent's stored hash
  directly instead of parsing the id out of the key string. See `e574fc7`.

- **`cortex/api/deps.py` — `require_hook_token` didn't accept the documented
  `X-Cortex-Hook-Token` header.** The `x_hook_token` parameter had no
  `alias`, so FastAPI mapped it to `X-Hook-Token` instead — but the README,
  `deploy/claude-code/settings.example.json`, and the function's own
  docstring all specify `X-Cortex-Hook-Token`. Every hook sink call using the
  documented header (SessionStart digest, `/hook/tool-use`,
  `/hook/session-end`) 401'd. Fixed with `alias="X-Cortex-Hook-Token"`. See
  `6a3ac0f`.

Both were only exposed by actually wiring up a real harness end-to-end
(Claude Code, agent id `claude-code`) rather than the `dsh`/`goose` agents
used in earlier testing, which don't have hyphens in their ids. Worth
checking `tests/test_security.py` and hook-sink tests for coverage of
hyphenated agent ids and the exact header name.
