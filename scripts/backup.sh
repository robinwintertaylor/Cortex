#!/usr/bin/env bash
# Nightly backup (FR-15): pg_dump + document for the restore drill.
# Cron:  15 3 * * *  /srv/cortex/scripts/backup.sh >> /var/log/cortex-backup.log 2>&1
#
# For RPO ≤ 5 min add WAL archiving to the db service:
#   command: >
#     postgres -c wal_level=replica -c archive_mode=on
#              -c archive_command='test ! -f /backups/wal/%f && cp %p /backups/wal/%f'
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-./backups}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"

echo "[$(date -u +%FT%TZ)] dump start"
docker compose exec -T db pg_dump -U cortex -d cortex -Fc \
  > "$BACKUP_DIR/cortex-$STAMP.dump"
echo "[$(date -u +%FT%TZ)] dump done: $BACKUP_DIR/cortex-$STAMP.dump ($(du -h "$BACKUP_DIR/cortex-$STAMP.dump" | cut -f1))"

# retention: 14 daily dumps
ls -1t "$BACKUP_DIR"/cortex-*.dump 2>/dev/null | tail -n +15 | xargs -r rm --

# restore drill (documented, run manually on a scratch host):
#   docker compose up -d db
#   cat backups/cortex-<stamp>.dump | docker compose exec -T db \
#     pg_restore -U cortex -d cortex --clean --if-exists
#   curl -s http://localhost:8738/healthz
# RTO target: consistent state < 1 h (FR-15 AC). Event-log replay
# (`cortex rebuild --from 0`) is the final fallback if projections diverge.
