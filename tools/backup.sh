#!/usr/bin/env bash
# Host-side helper for the `backup` compose service (see docs/backups.md).
#
#   tools/backup.sh run            run one backup now, inside the backup container
#   tools/backup.sh status         print /backups/latest.json (result of the last run)
#   tools/backup.sh list           list the archives in ./data/backups
#   tools/backup.sh verify [NAME]  mongorestore --dryRun + tar listing of one backup set
#                                  NAME: timestamp, archive file name or "latest" (default)
#   tools/backup.sh logs [N]       last N lines of the backup service log (default 50)
#
# Compose selection: set TT_OJ_COMPOSE_ARGS to the project/file flags, for staging
#   TT_OJ_COMPOSE_ARGS="-p tenstorrent-oj-staging -f compose.yaml -f compose.staging.yaml" tools/backup.sh run
# Production (plain `docker compose` in this checkout) needs nothing. The standard
# COMPOSE_PROJECT_NAME / COMPOSE_FILE variables work too, docker compose reads them itself.
set -euo pipefail
tt_oj_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$tt_oj_root"
# shellcheck disable=SC2206  # word-splitting the flags is the point
compose=(docker compose ${TT_OJ_COMPOSE_ARGS:-})

backup_exec() {
    # Run a command in the backup service; use a one-off container when it is not running.
    if [[ -n "$("${compose[@]}" ps -q --status running backup 2>/dev/null)" ]]; then
        "${compose[@]}" exec -T backup "$@"
    else
        mkdir -p data/backups
        "${compose[@]}" run --rm --no-deps -T backup "$@"
    fi
}

resolve_stamp() {
    # Turn "latest", a timestamp or an archive file name into the shared timestamp.
    local arg s
    arg=$(basename -- "${1:-latest}")
    case "$arg" in
        latest) s=$(backup_exec jq -r '.timestamp // empty' /backups/latest.json) ;;
        mongo-*.archive.gz) s=${arg#mongo-}; s=${s%.archive.gz} ;;
        files-*.tar.gz) s=${arg#files-}; s=${s%.tar.gz} ;;
        *) s=$arg ;;
    esac
    if [[ ! "$s" =~ ^[0-9]{8}T[0-9]{6}Z$ ]]; then
        printf 'Cannot resolve "%s" to a backup timestamp (expected e.g. 20261102T031500Z).\n' "${1:-latest}" >&2
        exit 1
    fi
    printf '%s\n' "$s"
}

case "${1:-}" in
    run)
        backup_exec bash /tools/backup-once.sh
        ;;
    status)
        backup_exec jq . /backups/latest.json
        ;;
    list)
        backup_exec ls -la --time-style=long-iso /backups
        ;;
    verify)
        stamp=$(resolve_stamp "${2:-latest}")
        printf '== mongorestore --dryRun of mongo-%s.archive.gz\n' "$stamp"
        if ! out=$(backup_exec mongorestore --host mongo --gzip --archive="/backups/mongo-$stamp.archive.gz" --drop --dryRun -v 2>&1); then
            printf '%s\n' "$out"
            exit 1
        fi
        printf '%s\n' "$out" | grep -vE 'found collection|archive prelude|using write concern'
        printf '%s collection(s) found in the archive:' "$(printf '%s\n' "$out" | grep -c 'bson to restore to')"
        printf '%s\n' "$out" | sed -n 's/.*found collection `\([^`]*\)` bson.*/\1/p' | sort | tr '\n' ' '
        printf '\n'
        printf '== tar listing of files-%s.tar.gz\n' "$stamp"
        backup_exec bash -c "tar --list --gzip --file=/backups/files-$stamp.tar.gz | wc -l | sed 's/\$/ entries/'"
        ;;
    logs)
        "${compose[@]}" logs --no-log-prefix --tail "${2:-50}" backup
        ;;
    *)
        printf 'Usage: %s {run|status|list|verify [NAME]|logs [N]}\n' "$0" >&2
        exit 2
        ;;
esac
