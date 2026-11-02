# CS540 TT-Lang problems: deployment runbook

Everything below runs on the OJ host from the deployed TTOJ checkout, with Docker
group membership (`sg docker` as in the other scripts).

## 1. Build the TT-Lang runtime image (once per tt-lang version)

```bash
./profiling-build/build-ttlang.sh            # builds tt-oj-ttlang-runtime:1.1.10 and opens device 0 once
```

The image installs the pinned full wheel
`tt_lang-1.1.10.dev20260921-cp312-cp312-manylinux_2_39_x86_64.whl` from
Tenstorrent's index (bundled ttnn with the device profiler enabled, SFPI via
`tt-lang-setup-sfpi`) on Ubuntu 24.04. To move to another version, change
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

## 3. Verify end to end

```bash
python3 tools/check_e2e.py --only "TT-Lang vector add" "TT-Lang ragged sum" "TT-Lang ragged softmax" \
    "TT-Lang wrong answer" "TT-Lang compile error" "TT-Lang runtime error"
```

Each accepted case takes roughly 10-20 s (host pass + device pass + counter pass, JIT included);
the largest vector case moves 3 × 64 MB and finishes in about 5 s per pass.

## 4. Operate the quota

`submission_limit` in each `problems/ttlang-*/metadata.json` (default 200) is
read by the site addon at start and enforced in `handler/before/ProblemSubmit#post`
by counting all `record` documents of that problem (every account, pretests
included; rejudges reuse the record and do not count). The problem page shows
`Submission quota: used / limit`. To raise a limit, edit the manifest and
restart the site container; to reset the count, delete records through Hydro's
administration tools.

## 5. Without Docker (development only)

```bash
python3 tools/ttlang_local_judge.py --problem ttlang-ragged-softmax \
    --solution problems/ttlang-ragged-softmax/accepted.py \
    --python /path/to/venv/bin/python --report softmax.json
```

This runs the same wrapper in subprocesses of the given interpreter (which must
have tt-lang installed), executes the host and device passes, and parses them
with the worker's own `metrics.py` and `device_profile.py`.
