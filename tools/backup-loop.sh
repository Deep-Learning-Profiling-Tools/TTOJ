#!/usr/bin/env bash
# Entrypoint of the `backup` compose service: run backup-once.sh now when
# BACKUP_RUN_ON_START=1, then every BACKUP_INTERVAL_HOURS (default 24).
# A failed run is logged by backup-once.sh and never stops the loop.
# SIGTERM (docker stop) interrupts the wait and exits cleanly. See docs/backups.md.
set -uo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
once=$here/backup-once.sh

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

interval_hours=${BACKUP_INTERVAL_HOURS:-24}
if [[ ! "$interval_hours" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    log "backup-loop: BACKUP_INTERVAL_HOURS must be a positive number (got '$interval_hours')"
    exit 2
fi
interval_s=$(awk -v h="$interval_hours" 'BEGIN { printf "%d", h * 3600 }')
if (( interval_s < 60 )); then
    log "backup-loop: BACKUP_INTERVAL_HOURS must be at least 1 minute (got '$interval_hours')"
    exit 2
fi
if [[ ! -r "$once" ]]; then
    log "backup-loop: $once is missing (is tools/backup-once.sh mounted?)"
    exit 2
fi

sleep_pid=
trap 'log "backup-loop: stopping"; [[ -n $sleep_pid ]] && kill "$sleep_pid" 2>/dev/null; exit 0' TERM INT

log "backup-loop: interval=${interval_hours}h retention=${BACKUP_RETENTION_DAYS:-14}d run_on_start=${BACKUP_RUN_ON_START:-0}"
if [[ "${BACKUP_RUN_ON_START:-0}" == "1" ]]; then
    bash "$once" || log "backup-loop: initial run failed (status $?)"
fi
while true; do
    log "backup-loop: next run at $(date -u -d "@$(( $(date +%s) + interval_s ))" +%Y-%m-%dT%H:%M:%SZ)"
    sleep "$interval_s" &
    sleep_pid=$!
    wait "$sleep_pid"
    sleep_pid=
    bash "$once" || log "backup-loop: run failed (status $?)"
done
