# Backups

The `backup` service in `compose.yaml` takes a backup of the online judge every
`BACKUP_INTERVAL_HOURS` (default 24) and keeps `BACKUP_RETENTION_DAYS` (default 14)
of history in `./data/backups`. It runs the stock `mongo:7-jammy` image as root, which
is what lets it read the root-owned `0700` job archives; the scripts it executes are
`tools/backup-loop.sh` (the schedule) and `tools/backup-once.sh` (one run), mounted
read-only from this checkout. `tools/backup.sh` and `tools/restore.sh` wrap the manual
operations from the host.

## What is backed up

Every run writes one pair of archives named by the same UTC timestamp:

| Archive | Contents | Source |
| --- | --- | --- |
| `mongo-<ts>.archive.gz` | `mongodump --gzip --archive` of every database on the `mongo` service: Hydro's `hydro` database (records, users, problems, domain settings, tokens) plus `admin`/`config`. | `mongo-data` volume |
| `files-<ts>.tar.gz` | `profiles/**` (public per-record profiling reports and `case-*.csv` device traces) and, from the private job archives, `jobs/<record>/attempts/<attempt>/report.json` and `events.jsonl` together with their directory entries, so owners and modes (`root 0700`, files `0400`) survive a restore. | `./data/profiles`, `./data/jobs` |

Deliberately skipped: `jobs/hardware.lock`, `jobs/worker-status.json`, and the
`case-*.csv`, `result.json`, `solution.*`, stdout/stderr and other per-attempt files
under `jobs/` (the device CSVs of published attempts already sit in `profiles/`, and
`result.json` duplicates `report.json`). On the staging instance ~1.5 GB of source data
gzips to a few tens of MB per run.

Not covered by this service: the `hydro-files` volume (`/data/files` in the site
container: problem testdata and uploads referenced from `hydro.storage`, ~5 MB on
staging) and `hydro-config`. `tools/backup-once.sh` includes `/hydro-files` in the
files archive automatically when that volume is mounted there; to enable it, add
`- hydro-files:/hydro-files:ro` to the backup service's volumes and set
`TT_OJ_HYDRO_FILES_VOLUME=<project>_hydro-files` when restoring.

## Where, permissions, retention

* Archives land in `./data/backups` (`/backups` in the container), which is gitignored
  with the rest of `./data`. They are `root:root 0600`: the database contains user
  records and the files archive contains private job reports. Create the directory
  yourself (`mkdir -p data/backups`) before the first `up` if you want it owned by your
  host user so that `ls` and `rm` work without `sudo`; the contents still need root or
  the container to read.
* `latest.json` (world-readable) records the last run: timestamp, file names, sizes,
  per-archive `ok`/`exit_code`/`error`, the names pruned by retention, and the free space
  of the backup directory.
* Retention: after a run in which **both** archives succeeded, `mongo-*.archive.gz` and
  `files-*.tar.gz` older than `BACKUP_RETENTION_DAYS` are deleted. A failing run never
  prunes, so a broken backup cannot erase the last good one. `BACKUP_RETENTION_DAYS=0`
  disables pruning. Half-written `*.partial` files from a killed run are removed after a
  day.
* The directory is on the same host as the data it protects. Copy it somewhere else
  (rsync, object storage) if you need protection against losing the machine.

## Configuration

Variables are read by `docker compose` from the shell or `.env` when the service is
created (`up -d backup` again after changing them):

| Variable | Default | Meaning |
| --- | --- | --- |
| `BACKUP_INTERVAL_HOURS` | `24` | Time between runs (decimals allowed, minimum 1 minute). The wait starts after the previous run ends. |
| `BACKUP_RETENTION_DAYS` | `14` | Age after which archives are pruned. |
| `BACKUP_RUN_ON_START` | `0` | `1` runs a backup as soon as the container starts (and again after every restart). |
| `SERVICE_CPUSET` | `0-3,8-11` | Same CPU pinning as the other services. |

Compose invocation: production is plain `docker compose` in this checkout; staging adds
`-p tenstorrent-oj-staging -f compose.yaml -f compose.staging.yaml`. The helper scripts
take those flags from `TT_OJ_COMPOSE_ARGS`:

```sh
export TT_OJ_COMPOSE_ARGS="-p tenstorrent-oj-staging -f compose.yaml -f compose.staging.yaml"   # staging only
```

## Running and checking

```sh
mkdir -p data/backups
docker compose $TT_OJ_COMPOSE_ARGS up -d backup            # start the schedule
BACKUP_RUN_ON_START=1 docker compose $TT_OJ_COMPOSE_ARGS up -d backup   # ... and back up right now

tools/backup.sh run        # one-off backup now (uses the running service, or a one-off container)
tools/backup.sh status     # latest.json
tools/backup.sh list       # ls -la of ./data/backups
tools/backup.sh logs 20    # one summary line per run, plus FAILED lines when something broke
```

A healthy log line looks like

```
2026-11-02T03:15:09Z backup 20261102T031500Z ok=true mongo=mongo-20261102T031500Z.archive.gz 123456B exit=0 files=files-20261102T031500Z.tar.gz 45678901B entries=812 exit=0 deleted=0 duration=9s
```

`ok=false` means at least one archive is missing; the next line says which and why, and
`latest.json` has the same detail. A mongodump failure never skips the files archive and
vice versa, and the loop keeps running. The one-off `tools/backup.sh run` exits non-zero
on failure, so it can be used from cron or a monitoring check.

## Verifying a backup

```sh
tools/backup.sh verify            # the set named in latest.json
tools/backup.sh verify 20261102T031500Z
```

This runs `mongorestore --dryRun --drop` against the archive inside the container
(reads the whole archive, writes nothing, prints the namespaces it would restore) and
lists the files archive. `tools/backup-once.sh` already runs `gzip -t` on the dump and a
full `tar --list` on the files archive before it renames them from `.partial`, so an
archive that exists under its final name has at least been read back once.

For a full rehearsal without touching the live data, restore the database into a scratch
name and compare collection counts:

```sh
docker compose $TT_OJ_COMPOSE_ARGS exec backup mongorestore --host mongo --gzip \
    --archive=/backups/mongo-<ts>.archive.gz --nsInclude='hydro.*' --nsFrom='hydro.*' --nsTo='hydro_verify.*'
docker compose $TT_OJ_COMPOSE_ARGS exec mongo mongosh --quiet --eval \
    'db.getSiblingDB("hydro_verify").getCollectionNames().forEach(c => print(c, db.getSiblingDB("hydro_verify")[c].countDocuments()))'
docker compose $TT_OJ_COMPOSE_ARGS exec mongo mongosh --quiet --eval 'db.getSiblingDB("hydro_verify").dropDatabase()'
```

## Restoring

Restores are destructive; take a fresh backup first (`tools/backup.sh run`) so the
state you are replacing is itself recoverable.

### Database

```sh
tools/restore.sh mongo 20261102T031500Z --dry-run     # check the archive, change nothing
tools/restore.sh mongo 20261102T031500Z               # mongorestore --drop of every database in the archive
tools/restore.sh mongo latest --ns 'hydro.*'          # only Hydro's database
docker compose $TT_OJ_COMPOSE_ARGS restart site
```

`tools/restore.sh mongo` runs, inside the backup container,
`mongorestore --host mongo --gzip --archive=/backups/mongo-<ts>.archive.gz --drop`
(`--nsInclude=<pattern>` when `--ns` is given). `--drop` replaces each collection
present in the archive; collections created after the backup and absent from it are
left alone.

**Restart the site afterwards.** Hydro keeps settings, domains, user sessions and
other state cached in memory; a restore under a running site leaves it serving the
old cache until `docker compose ... restart site`. Logged-in sessions created after the
backup disappear (their tokens are gone), and the worker's judge login is re-established
by its own reconnect logic. Submissions judged between the backup and the restore are
lost from the database but their profiling files stay in `./data/profiles` and
`./data/jobs` unless you also restore the files archive.

### Files

```sh
docker compose $TT_OJ_COMPOSE_ARGS stop worker          # it writes to data/jobs and data/profiles
tools/restore.sh files 20261102T031500Z --dry-run        # count what would be extracted
tools/restore.sh files 20261102T031500Z
docker compose $TT_OJ_COMPOSE_ARGS start worker
```

The files restore runs a throw-away root `mongo:7-jammy` container with
`./data/backups:ro`, `./data/profiles` and `./data/jobs` mounted and extracts with
`tar --extract --directory=/ --preserve-permissions --same-owner`, so the job archives
come back as `root 0700`/`0400` and the public profiles as before. Existing files with
the same path are overwritten; files that only exist on disk are kept. The script
refuses to run while the worker is up unless `--force` is given. The site reads
profiles read-only and needs no restart for this.

### Starting from an empty machine

Clone the repository, recreate `.env`, copy the two archives into `./data/backups`
(`sudo install -m 600 -o root ...`), start `mongo` and `backup`
(`docker compose up -d mongo backup`), run the database restore, then the files
restore, then `docker compose up -d` for the rest. Problem statements come from
`./problems` and are re-imported by the site's bootstrap; the `hydro-files` volume
(testdata uploads) is not in these archives unless you enabled the optional mount above.

## Caveats

* Consistency: `mongodump` reads collections one after another without a snapshot, and
  the files archive is taken while the worker may be writing. A record whose judging
  finishes mid-run can appear in the database but not yet in the files archive (or the
  other way round). `tar` keeps going when a file changes or disappears while it is
  read and notes the count in `latest.json` under `files.warnings`.
* The schedule is relative to container start: with `BACKUP_INTERVAL_HOURS=24` and no
  `BACKUP_RUN_ON_START`, the first backup happens 24 h after `up`. Set
  `BACKUP_RUN_ON_START=1` to get one immediately. Runs do not overlap: a manual
  `tools/backup.sh run` while the scheduled one is busy exits with status 3.
* `docker stop backup` waits up to two minutes (`stop_grace_period`) for a run in
  progress; a run killed anyway leaves `*.partial` files that are cleaned up later.
* Run `mongodump`/`mongorestore` through the backup service (`docker compose ... exec backup`
  or the helper scripts), not as the first argument of a bare `docker run mongo:7-jammy ...`:
  the image's entrypoint drops any `mongo*` command to the unprivileged `mongodb` user,
  which cannot read the `0600` archives. If you must use `docker run`, pass
  `--entrypoint mongorestore`.
* Restoring onto a different Hydro version than the one that produced the dump may
  need Hydro's own migration scripts; keep the `TTOJ_SOURCE_REF` build argument in sync.
