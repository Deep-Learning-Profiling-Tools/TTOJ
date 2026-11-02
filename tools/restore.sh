#!/usr/bin/env bash
# Restore a backup written by the `backup` compose service (see docs/backups.md).
#
#   tools/restore.sh mongo NAME [--dry-run] [--ns PATTERN]
#       mongorestore --drop from data/backups/mongo-<ts>.archive.gz, run inside the
#       backup container. --ns limits the restore to a namespace pattern, e.g. 'hydro.*'.
#       Restart the site afterwards: it caches settings, domains and sessions.
#   tools/restore.sh files NAME [--dry-run] [--force]
#       extract data/backups/files-<ts>.tar.gz over ./data/profiles and ./data/jobs with
#       owners and modes preserved. Refuses to run while the worker is up unless --force.
#
# NAME is a timestamp (20261102T031500Z), an archive file name, or "latest".
# Compose selection via TT_OJ_COMPOSE_ARGS, exactly as in tools/backup.sh.
set -euo pipefail
tt_oj_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$tt_oj_root"
# shellcheck disable=SC2206
compose=(docker compose ${TT_OJ_COMPOSE_ARGS:-})
image=mongo:7-jammy

backup_exec() {
    if [[ -n "$("${compose[@]}" ps -q --status running backup 2>/dev/null)" ]]; then
        "${compose[@]}" exec -T backup "$@"
    else
        "${compose[@]}" run --rm --no-deps -T backup "$@"
    fi
}

resolve_stamp() {
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

mode=${1:-}
name=${2:-}
dry_run=0 force=0 ns=""
shift $(( $# >= 2 ? 2 : $# ))
while (( $# )); do
    case "$1" in
        --dry-run) dry_run=1 ;;
        --force) force=1 ;;
        --ns) ns=${2:?--ns needs a pattern}; shift ;;
        *) printf 'Unknown option: %s\n' "$1" >&2; exit 2 ;;
    esac
    shift
done

case "$mode" in
    mongo)
        stamp=$(resolve_stamp "$name")
        archive=/backups/mongo-$stamp.archive.gz
        backup_exec test -f "$archive" || { printf '%s not found in ./data/backups\n' "$archive" >&2; exit 1; }
        args=(mongorestore --host mongo --gzip --archive="$archive" --drop)
        [[ -n "$ns" ]] && args+=(--nsInclude="$ns")
        (( dry_run )) && args+=(--dryRun)
        printf '== %s\n' "${args[*]}"
        backup_exec "${args[@]}"
        if (( ! dry_run )); then
            printf '\nDatabase restored. Restart the site so it drops cached state:\n  %s restart site\n' "${compose[*]}"
        fi
        ;;
    files)
        stamp=$(resolve_stamp "$name")
        archive=data/backups/files-$stamp.tar.gz
        [[ -e "$archive" ]] || { printf '%s not found\n' "$archive" >&2; exit 1; }
        if (( ! dry_run && ! force )) && [[ -n "$("${compose[@]}" ps -q --status running worker 2>/dev/null)" ]]; then
            printf 'The worker is running and writes to data/jobs and data/profiles. Stop it first:\n  %s stop worker\nor pass --force.\n' "${compose[*]}" >&2
            exit 1
        fi
        mounts=(-v "$tt_oj_root/data/backups:/backups:ro"
                -v "$tt_oj_root/data/profiles:/profiles"
                -v "$tt_oj_root/data/jobs:/jobs")
        excludes=()
        if [[ -n "${TT_OJ_HYDRO_FILES_VOLUME:-}" ]]; then
            mounts+=(-v "$TT_OJ_HYDRO_FILES_VOLUME:/hydro-files")
        else
            excludes=(--exclude=hydro-files --exclude='hydro-files/*')
        fi
        if (( dry_run )); then
            docker run --rm "${mounts[@]}" "$image" tar --list --gzip --file="/backups/files-$stamp.tar.gz" \
                ${excludes[@]+"${excludes[@]}"} | wc -l | sed 's/$/ entries would be extracted/'
        else
            printf '== extracting %s over ./data/profiles and ./data/jobs\n' "$archive"
            docker run --rm "${mounts[@]}" "$image" tar --extract --gzip --file="/backups/files-$stamp.tar.gz" \
                --directory=/ --preserve-permissions --same-owner ${excludes[@]+"${excludes[@]}"}
            printf 'Files restored. Start the worker again if you stopped it:\n  %s start worker\n' "${compose[*]}"
        fi
        ;;
    *)
        printf 'Usage: %s mongo NAME [--dry-run] [--ns PATTERN]\n       %s files NAME [--dry-run] [--force]\n' "$0" "$0" >&2
        exit 2
        ;;
esac
