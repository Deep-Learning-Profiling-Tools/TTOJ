# CS540 TT-Lang problems: deployment runbook

Everything below runs on the OJ host from the deployed TTOJ checkout, with Docker
group membership (`sg docker` as in the other scripts).

## 1. Build the TT-Lang runtime image (once per tt-lang version)

```bash
./profiling-build/build-ttlang.sh            # builds tt-oj-ttlang-runtime:1.1.10, smoke-tests one chip, exports the image
```

The image installs the pinned full wheel
`tt_lang-1.1.10.dev20260921-cp312-cp312-manylinux_2_39_x86_64.whl` (bundled
ttnn with the device profiler enabled, SFPI via `tt-lang-setup-sfpi`) on
Ubuntu 24.04. The script keeps a copy of the wheel in
`profiling-build/wheels/` (gitignored, 174 MB) and checks it against
`profiling-build/wheels.sha256`; the Dockerfile installs that copy when present
and only falls back to Tenstorrent's index when it is missing, so the image can
be rebuilt even if the index moves or drops the file. The script also exports
the finished image to `data/images/tt-oj-ttlang-runtime-1.1.10.tar.gz`
(about 700 MB); a machine without index access can load it with
`docker load -i` instead of building. To move to another version, change
`TTLANG_WHEEL_URL` in `profiling-build/ttlang.Dockerfile`, rebuild, and update
the version named in `tools/generate_ttlang_problems.py` statements. Use the
exact wheel URL: a `==` version selector also matches the `+light` wheel, which
has no ttnn.

## 2. Rebuild the worker and the site

```bash
docker compose build worker site
docker compose up -d worker site          # the site re-reads /problems on start and seeds new ids
```

The worker image now carries `sandbox/ttlang/runner.py` as `/app/ttlang_runner.py`
and advertises the `ttlang` language to Hydro. The site addon registers the
`ttlang` language entry and the `submission_limit` hook; `/problems` is bind
mounted, so the three new folders are seeded on the next site start
(existing problems are never overwritten; `site/reseed.sh` forces a reseed).

The worker judges up to three submissions at once, one per Blackhole chip
(`TT_OJ_CHIPS`, default `0,1,2`; `TT_OJ_CONCURRENCY` defaults to the pool
size; see `docs/evaluation-protocol.md`, "Scheduling"). Chip 3 is never
touched by the judge and stays free for other users of the machine. A
TT-Lang submission runs in two containers: one judges every testcase, the
other reads the hardware counters of the accepted ones; an accepted
solution takes about a minute. `TT_OJ_HIDDEN_PROBLEMS` in `.env` (the
CS540 instance sets `TT1001,TT1002,TT2001,TT2002`) hides the TTNN and
Metalium demos, which need the whole mesh, from students. Restart the site only when the
queue is empty: records that are being judged while the site process restarts
stay "Judging" and must be rejudged by hand. Restarting the worker is safe;
Hydro re-queues its in-flight records.

## 3. Verify end to end

```bash
python3 tools/check_e2e.py --only "TT-Lang vector add" "TT-Lang ragged sum" "TT-Lang ragged softmax" \
    "TT-Lang wrong answer" "TT-Lang compile error" "TT-Lang runtime error"
```

Each accepted case takes roughly 10-20 s (host pass + device pass + counter pass, JIT included);
the largest vector case moves 3 × 64 MB and finishes in about 5 s per pass.

## 4. Operate the quota

`submission_limit` in each `problems/ttlang-*/metadata.json` (default 200) is
read by the site addon at start and enforced in `handler/before/ProblemSubmit#post`.
The cap applies **per account and per problem**: the addon counts the `record`
documents of that problem with the submitting account's `uid` (pretests
included; rejudges reuse the record and do not count), so one student
exhausting their 200 does not affect anyone else. The problem page shows
`Submission quota: your account has used X / limit`, and `/leaderboard` shows
the viewer's own count in each problem's header.

To inspect a student's count as the instructor (or any account with
`PRIV_READ_RECORD_CODE`):

- open any of the student's records for that problem (`/record?uidOrName=<uname>&pid=<docId>`,
  then a row): the "Submissions used" tile on the record page shows that
  account's count for that problem (students see this tile only on their own
  records);
- `/leaderboard` lists a "Submissions" column for every account that has an
  accepted submission with device timing;
- or count directly in MongoDB (`docId` is the problem's numeric id, e.g. 6 for TT3001):

  ```sh
  docker exec <project>-mongo-1 mongosh --quiet --eval \
    'db.getSiblingDB("hydro").record.countDocuments({domainId:"system", pid:6, uid:<uid>})'
  ```

To raise a limit, edit the manifest and restart the site container; to give a
single account more attempts, delete some of its records for that problem
through Hydro's administration tools (the count is recomputed on every
submission).

## 5. Student accounts, guide and backups

Registration is disabled. Create accounts from a roster CSV
(`email,username,password[,displayName]`, one student per line; keep the file
outside the repository, for example `data/accounts/`, which is ignored by git)
with `tools/import-accounts.js`, which runs inside the site container so that
no password passes through a command line:

```bash
S=tenstorrent-oj-site-1   # or tenstorrent-oj-staging-site-1
docker cp tools/import-accounts.js $S:/tmp/import-accounts.js
docker cp data/accounts/roster.csv $S:/tmp/roster.csv
docker exec $S sh -c 'cd /opt/hydro && hydrooj cli execute "await require(\"/tmp/import-accounts.js\")(\"/tmp/roster.csv\")"'
docker exec $S rm /tmp/roster.csv /tmp/import-accounts.js
```

It prints `username -> uid` for created accounts, skips usernames that already
exist (their passwords are not reset) and lists failures. The CS540 roster
(16 accounts, GMU NetID as username, `netid@gmu.edu` as e-mail, the student's
G number as the initial password) was imported this way on 2026-11-03; students
change the password themselves through **Change password** in the top bar
(`/home/security`, behind Hydro's "Confirm Access" step).

The import stores the salt of each account's roster password as `ttInitialSalt`.
Hydro draws a new salt whenever a password is set, so while the two are equal the
account still uses the handed-out password and every page shows it a red notice
with a link to the change form. Re-running the import on the same roster creates
nobody and marks existing accounts whose password still equals the roster
password (this marked the 16 CS540 accounts on 2026-11-03). To reset a forgotten
password and show the notice again until the student changes it:

```bash
docker exec $S sh -c 'cd /opt/hydro && hydrooj cli execute "const u=global.Hydro.model.user; const x=await u.getByUname(\"system\", \"NETID\"); await u.setPassword(x._id, \"TEMP-PASSWORD\"); await u.setById(x._id, {ttInitialSalt: (await u.getById(\"system\", x._id))._salt})"'
```

Single accounts can also be made at
`/manage/userimport` or with
`hydrooj cli execute "await global.Hydro.model.user.create('email', 'uname', 'password')"`.
Hand out [`docs/student-guide.md`](student-guide.md). Daily database and report
backups are described in [`docs/backups.md`](backups.md).

## 6. Going live behind the existing tunnel

Students reach the course OJ at `http://98.93.3.136:28888/`. That address is a
reverse SSH tunnel that jgu7's account on the TT machine keeps open to an AWS
host (`ssh -R 0.0.0.0:28888:127.0.0.1:8888 ubuntu@ec2-98-93-3-136...`): every
visitor of port 28888 on the AWS host is forwarded to port 8888 on the TT
machine, where jgu7's `main`-branch deployment (`/home/jgu7/work/tenstorrent-oj`,
run as root) listens. Only one program can own port 8888, so the switch to this
branch needs one action by jgu7 (or any root user), either:

- **Re-aim the tunnel** (nothing else changes): replace `127.0.0.1:8888` by
  `127.0.0.1:18888` in the tunnel command (systemd unit or script) and restart
  that one process. The staging deployment in `/home/bcui2/research/TTOJ-staging`
  (compose project `tenstorrent-oj-staging`, port 18888, `restart: unless-stopped`,
  so it survives reboots and logouts) becomes the course OJ immediately.
- **Or hand over port 8888**: stop the old deployment
  (`docker compose down` in `/home/jgu7/work/tenstorrent-oj`, which keeps its
  volumes), then change `127.0.0.1:18888:8888` to `127.0.0.1:8888:8888` in
  `compose.staging.yaml` and run `docker compose -p tenstorrent-oj-staging -f
  compose.yaml -f compose.staging.yaml up -d site`. The tunnel is untouched.

Either way the old deployment's accounts and records do not carry over (it has
no TT-Lang problems). Merging PR #1 into `main` is independent of this switch:
a running deployment only changes when somebody rebuilds it from the merged
checkout (`git pull && docker compose build worker site backup && docker compose
up -d`), and that checkout must then set `TT_OJ_CHIPS`, `TT_OJ_HIDDEN_PROBLEMS`
and the accounts as described above.

## 7. Without Docker (development only)

```bash
python3 tools/ttlang_local_judge.py --problem ttlang-ragged-softmax \
    --solution problems/ttlang-ragged-softmax/accepted.py \
    --python /path/to/venv/bin/python --report softmax.json
```

This runs the same wrapper in subprocesses of the given interpreter (which must
have tt-lang installed), executes the host and device passes, and parses them
with the worker's own `metrics.py` and `device_profile.py`.
