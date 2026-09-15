#!/bin/sh
# The backup schedule for the prod profile (CLAUDE.md §8.4, ADR-0012). Rendered from
# slas_deploy.pgbackrest; a unit test keeps file and code in step. One loop, no cron
# daemon: a full backup when the day matches BACKUP_FULL_CRON's weekday, a differential
# every other day at BACKUP_DIFF_CRON's hour, `pgbackrest check` every hour, and a Qdrant
# snapshot next to every backup. Every run writes one line to Backups/backup-journal.jsonl.
set -eu
STANZA="${PGBACKREST_STANZA:-slas}"
JOURNAL="${SLAS_DATA_ROOT:-/data}/Backups/backup-journal.jsonl"
FULL_DAY="${BACKUP_FULL_CRON:-0}"     # weekday 0-6 (Sunday = 0)
AT_HOUR="${BACKUP_DIFF_CRON:-2}"       # hour of day, 0-23
mkdir -p "$(dirname "$JOURNAL")"
record() {
  printf '{"at":"%s","kind":"%s","ok":%s,"seconds":%s}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2" "$3" >> "$JOURNAL"
}
run_backup() {
  start=$(date +%s)
  if pgbackrest --stanza="$STANZA" --type="$1" backup; then ok=true; else ok=false; fi
  record "backup-$1" "$ok" "$(( $(date +%s) - start ))"
  if [ -d "${SLAS_DATA_ROOT:-/data}/qdrant" ]; then
    tar -C "${SLAS_DATA_ROOT:-/data}" -czf "${SLAS_DATA_ROOT:-/data}/Backups/qdrant/qdrant-$(date -u +%Y%m%dT%H%M%SZ).tgz" qdrant 2>/dev/null || true
  fi
}
mkdir -p "${SLAS_DATA_ROOT:-/data}/Backups/qdrant"
pgbackrest --stanza="$STANZA" stanza-create 2>/dev/null || true
last_backup_day=""
while true; do
  now_hour=$(date -u +%H); now_day=$(date -u +%Y-%m-%d); weekday=$(date -u +%w)
  if [ "$now_hour" -eq "$AT_HOUR" ] && [ "$now_day" != "$last_backup_day" ]; then
    if [ "$weekday" -eq "$FULL_DAY" ]; then run_backup full; else run_backup diff; fi
    last_backup_day="$now_day"
  fi
  start=$(date +%s)
  if pgbackrest --stanza="$STANZA" check >/dev/null 2>&1; then ok=true; else ok=false; fi
  record check "$ok" "$(( $(date +%s) - start ))"
  sleep 3600
done
