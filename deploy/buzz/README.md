# Buzz bridge (optional, P2 — NG2: no Hive comms in v1)

Buzz speaks Nostr, not MCP — the v1 seam is a one-way bridge only: a cron job
posts the daily digest into a Buzz channel so the owner sees it where they
already are (risk table: "digest reaches owner where they are").

```bash
# daily digest → buzz channel (requires buzz-cli + BUZZ_PRIVATE_KEY)
17 8 * * *  brain digest 24h | buzz-cli post --channel "$BUZZ_CHANNEL" -f -
```

The API seams stay open for Plan 3 (Hive): every event is exportable
(`/v1/brain_recent`, `cortex export --markdown`), and Buzz events could be
indexed back into the brain the same way. No comms platform ships in v1 (D-001).
