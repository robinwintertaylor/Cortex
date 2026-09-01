# Scripts / cron wiring (PRD §10)

## Nightly research sweep (FR-11)

```cron
# research sweep — capture watched URLs nightly
30 4 * * * cd /srv/cortex && cortex sweep research-urls.txt --agent sweep
```

`research-urls.txt` is one URL per line. Captures land as notes + research
events and are extracted by the librarian.

Or via REST (any key):

```bash
curl -sf -X POST -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"urls": ["https://example.com/article"], "project": "research"}' \
  http://cortex.local:8738/v1/brain_sweep
```

## Backups (FR-15)

```cron
15 3 * * * cd /srv/cortex && scripts/backup.sh >> backups/backup.log 2>&1
```

For RPO ≤ 5 min, add WAL archiving to the db service command (see
scripts/backup.sh header). Restore drill documented there; target RTO < 1 h.

## Event-log replay (final fallback)

```bash
docker compose stop librarian
cortex rebuild --from 0      # replays every event; reproduces the same facts
docker compose start librarian
```
