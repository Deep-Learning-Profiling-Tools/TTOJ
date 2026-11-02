#!/usr/bin/env bash
# One backup run. Executes inside the `backup` compose service (image mongo:7-jammy),
# which runs as root so it can read the root-owned 0700 job archives.
#
#   1. mongodump of every database on the `mongo` service -> /backups/mongo-<UTC>.archive.gz
#   2. tar of /profiles plus jobs/*/attempts/*/{report.json,events.jsonl}
#      (directory entries included so modes/owners survive a restore) -> /backups/files-<UTC>.tar.gz
#   3. prune archives older than BACKUP_RETENTION_DAYS (only after a fully successful run)
#   4. write /backups/latest.json and log one summary line to stdout
#
# Each step records its own result; a failing mongodump never skips the files archive.
# Exit status: 0 when both archives were written and verified, 1 when either failed,
# 3 when another run still holds the lock. See docs/backups.md.
set -uo pipefail
umask 077   # archives hold user records and private job reports: root-only (0600)

backup_dir=${BACKUP_DIR:-/backups}
profiles_dir=${PROFILES_DIR:-/profiles}
jobs_dir=${JOBS_DIR:-/jobs}
hydro_files_dir=${HYDRO_FILES_DIR:-/hydro-files}   # optional extra mount (docs/backups.md)
mongo_host=${MONGO_HOST:-mongo}
retention_days=${BACKUP_RETENTION_DAYS:-14}

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

if [[ ! "$retention_days" =~ ^[0-9]+$ ]]; then
    log "backup error: BACKUP_RETENTION_DAYS must be a non-negative integer (got '$retention_days')"
    exit 1
fi
if [[ ! -d "$backup_dir" ]]; then
    log "backup error: $backup_dir is not a directory"
    exit 1
fi

exec 9>"$backup_dir/.lock"
if ! flock -n 9; then
    log "backup skipped: another run holds $backup_dir/.lock"
    exit 3
fi

stamp=$(date -u +%Y%m%dT%H%M%SZ)
started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
started_s=$(date +%s)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# ---- 1. MongoDB ---------------------------------------------------------------
mongo_name=mongo-$stamp.archive.gz
mongo_partial=$backup_dir/$mongo_name.partial
mongo_ok=false mongo_bytes=0 mongo_error=""
# Progress goes to the scratch log; only a failure surfaces it (stdout stays one line per run).
mongodump --host "$mongo_host" --gzip --archive="$mongo_partial" >"$work/mongodump.log" 2>&1
mongo_exit=$?
if (( mongo_exit == 0 )) && gzip -t "$mongo_partial" 2>>"$work/mongodump.log"; then
    mv -f "$mongo_partial" "$backup_dir/$mongo_name"
    mongo_bytes=$(stat -c %s "$backup_dir/$mongo_name")
    mongo_ok=true
else
    (( mongo_exit == 0 )) && mongo_exit=1   # dump "succeeded" but the archive is not valid gzip
    rm -f "$mongo_partial"
    mongo_error=$(tail -n 5 "$work/mongodump.log" | tr '\n' ' ' | sed 's/ *$//')
    mongo_error=${mongo_error:-"mongodump exited with status $mongo_exit"}
fi

# ---- 2. Files -----------------------------------------------------------------
files_name=files-$stamp.tar.gz
files_partial=$backup_dir/$files_name.partial
files_ok=false files_bytes=0 files_entries=0 files_exit=0 files_error="" files_warnings=0
list=$work/files.list
(
    cd / || exit 1
    # Public profiling reports: everything below /profiles.
    find "${profiles_dir#/}" -mindepth 1 -print0
    # Private job archives: the directory skeleton plus report.json / events.jsonl only.
    # hardware.lock, worker-status.json, case-*.csv and the other attempt files are skipped
    # (the CSVs of published attempts are already in /profiles).
    j=${jobs_dir#/}
    find "$j" -mindepth 1 -maxdepth 1 -type d -print0
    find "$j" -mindepth 2 -maxdepth 3 -type d -path "$j/*/attempts*" -print0
    find "$j" -mindepth 4 -maxdepth 4 -type f -path "$j/*/attempts/*/*" \
        \( -name report.json -o -name events.jsonl \) -print0
    # Optional: Hydro's uploaded-files volume, when mounted at /hydro-files.
    if [[ -d "$hydro_files_dir" ]]; then
        find "${hydro_files_dir#/}" -mindepth 1 -print0
    fi
) >"$list" 2>"$work/find.log"

if [[ ! -s "$list" ]]; then
    files_exit=1
    files_error="nothing to archive (are $profiles_dir and $jobs_dir mounted?) $(tr '\n' ' ' <"$work/find.log")"
else
    ( cd / && tar --create --gzip --file="$files_partial" --no-recursion --null --files-from="$list" \
        --ignore-failed-read --warning=no-file-changed --warning=no-file-removed ) >"$work/tar.log" 2>&1
    files_exit=$?
    # tar exit 1 = "some files differ" (changed while being read): keep the archive, note it.
    if (( files_exit <= 1 )) && files_entries=$(tar --list --gzip --file="$files_partial" 2>>"$work/tar.log" | wc -l) \
        && (( files_entries > 0 )); then
        mv -f "$files_partial" "$backup_dir/$files_name"
        files_bytes=$(stat -c %s "$backup_dir/$files_name")
        files_warnings=$(wc -l <"$work/tar.log")
        files_ok=true
    else
        (( files_exit == 0 )) && files_exit=1
        rm -f "$files_partial"
        files_error=$(tail -n 5 "$work/tar.log" | tr '\n' ' ' | sed 's/ *$//')
        files_error=${files_error:-"tar exited with status $files_exit"}
    fi
fi

# ---- 3. Retention -------------------------------------------------------------
ok=false
$mongo_ok && $files_ok && ok=true
deleted_json='[]'
if $ok && (( retention_days > 0 )); then
    # Only prune after a successful run, so a broken backup never erases the last good one.
    deleted_json=$(find "$backup_dir" -maxdepth 1 -type f \
        \( -name 'mongo-*.archive.gz' -o -name 'files-*.tar.gz' \) \
        -mmin +"$(( retention_days * 1440 ))" -printf '%f\n' -delete | jq -R . | jq -s .)
fi
# Leftovers of a run that was killed half-way.
find "$backup_dir" -maxdepth 1 -type f -name '*.partial' -mmin +1440 -delete 2>/dev/null

# ---- 4. Summary ---------------------------------------------------------------
finished_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
duration=$(( $(date +%s) - started_s ))
free_bytes=$(df -Pk "$backup_dir" | awk 'NR == 2 { print $4 * 1024 }')
jq -n \
    --arg timestamp "$stamp" --arg started_at "$started_at" --arg finished_at "$finished_at" \
    --argjson duration_seconds "$duration" --argjson ok "$ok" \
    --arg mongo_file "$mongo_name" --argjson mongo_ok "$mongo_ok" --argjson mongo_exit "$mongo_exit" \
    --argjson mongo_bytes "$mongo_bytes" --arg mongo_error "$mongo_error" \
    --arg files_file "$files_name" --argjson files_ok "$files_ok" --argjson files_exit "$files_exit" \
    --argjson files_bytes "$files_bytes" --argjson files_entries "$files_entries" \
    --argjson files_warnings "$files_warnings" --arg files_error "$files_error" \
    --argjson retention_days "$retention_days" --argjson deleted "$deleted_json" \
    --argjson free_bytes "${free_bytes:-0}" \
    '{
        timestamp: $timestamp, started_at: $started_at, finished_at: $finished_at,
        duration_seconds: $duration_seconds, ok: $ok,
        mongo: { file: (if $mongo_ok then $mongo_file else null end), ok: $mongo_ok,
                 exit_code: $mongo_exit, bytes: $mongo_bytes,
                 error: (if $mongo_error == "" then null else $mongo_error end) },
        files: { file: (if $files_ok then $files_file else null end), ok: $files_ok,
                 exit_code: $files_exit, bytes: $files_bytes, entries: $files_entries,
                 warnings: $files_warnings,
                 error: (if $files_error == "" then null else $files_error end) },
        retention_days: $retention_days, deleted: $deleted,
        backup_dir_free_bytes: $free_bytes
    }' >"$work/latest.json"
# latest.json carries only names, sizes and status, so it is world-readable for monitoring.
install -m 644 "$work/latest.json" "$backup_dir/latest.json.tmp" \
    && mv -f "$backup_dir/latest.json.tmp" "$backup_dir/latest.json"

log "backup $stamp ok=$ok mongo=$mongo_name ${mongo_bytes}B exit=$mongo_exit" \
    "files=$files_name ${files_bytes}B entries=$files_entries exit=$files_exit" \
    "deleted=$(jq length <<<"$deleted_json") duration=${duration}s"
$mongo_ok || log "backup $stamp mongodump FAILED: $mongo_error"
$files_ok || log "backup $stamp files archive FAILED: $files_error"
$ok
