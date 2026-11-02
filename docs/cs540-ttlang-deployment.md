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

The worker judges up to four submissions at once, one per Blackhole chip
(`TT_OJ_CHIP_COUNT`, `TT_OJ_CONCURRENCY`, default 4; see
`docs/evaluation-protocol.md`, "Scheduling"). Restart the site only when the
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

Registration is disabled. Create accounts as the administrator at
`/manage/userimport` (one `email,username,password` line per student, preview,
import) or with
`hydrooj cli execute "await global.Hydro.model.user.create('email', 'uname', 'password')"`
inside the site container, and tell students to change the password at
`/home/security`. Hand out [`docs/student-guide.md`](student-guide.md) after
filling in the OJ URL. Daily database and report backups are described in
[`docs/backups.md`](backups.md).

## 6. Without Docker (development only)

```bash
python3 tools/ttlang_local_judge.py --problem ttlang-ragged-softmax \
    --solution problems/ttlang-ragged-softmax/accepted.py \
    --python /path/to/venv/bin/python --report softmax.json
```

This runs the same wrapper in subprocesses of the given interpreter (which must
have tt-lang installed), executes the host and device passes, and parses them
with the worker's own `metrics.py` and `device_profile.py`.
