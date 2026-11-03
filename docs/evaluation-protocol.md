# Evaluation and research data protocol

## Current policy

The executable policy is [`worker/evaluation-policy.json`](../worker/evaluation-policy.json),
named **`tt-oj-diagnostic-v1`**, pinned in `worker/protocol.py` to SHA-256:

```text
429717d46515f57149cfc53645d17b43403877379feed1a02365f9286b52e0cf
```

The worker refuses to start if the policy bytes differ from that pin. Change the
version and checksum together when changing a measurement recipe. Every new
attempt archives the exact policy bytes and includes its ID, hash and parsed
contents in the report. Observed host/device warmup and sample counts are checked
against the policy; a mismatch appears in `cases[].policy_validation` and quality
reason codes. This is a diagnostic protocol. **Formal ranking is disabled.**

Reports created by this implementation use `schema_version: 2`. Existing schema-1
reports remain historical observations; missing source, policy, queue timestamps
or lifecycle events are not reconstructed retrospectively.

## What the two measurements mean

| Measurement | Recipe | Included | Excluded |
| --- | --- | --- | --- |
| Host synchronized call | ttnn/Metalium: fresh container per case; first correctness/JIT invocation, 5 warmups, 50 timed repeats; profiler disabled. TT-Lang: one container for all cases, profiler enabled; 3 warmups, 7 timed repeats around the invocations whose device cycles are captured | TTNN `solve(a,b)` followed by device synchronization, including allocations inside `solve`; Metalium `EnqueueMeshWorkload` followed by `distributed::Finish` | Container startup, device initialization, first-call JIT, input upload and output download |
| Device kernel cycles | ttnn/Metalium: separate fresh container per accepted case; profiler enabled; first invocation, 5 warmups, 10 selected repeats. TT-Lang: the same run as correctness and host timing; first invocation, 3 warmups, 7 selected repeats | Official firmware kernel zones, grouped by device/core/RISC and ordered stage occurrence; same-core envelopes | Initial invocation and warmup events are excluded from selected statistics; profiler flush/export is outside kernel cycle intervals |

For the CS540 TT-Lang problems (`ttlang` mode) both recipes apply unchanged to the
submitted `@ttl.operation` call: the host pass times `operation(inputs..., out)` plus
device synchronization on preallocated FP32 tile tensors (padding, upload and readback
excluded). From the device pass the site derives the **device time** of each case:
per selected sample, first kernel start to last kernel end over all cores and RISCs
(tt-metal's DEVICE KERNEL DURATION definition), median over the ten samples. NoC event
tracing is left off because it logs every transfer and perturbs kernel time. The
`/leaderboard` page ranks each account's fastest accepted submission by the geometric
mean of device time over the testcases; host timings and wall time are never ranked.
Cross-core spans assume the per-core cycle counters are aligned; tt-metal documents
minor skew between cores.

For TT-Lang, a second container (1 warmup, 3 selected samples per accepted case) runs with
`TT_METAL_PROFILE_PERF_COUNTERS=39` (FPU, PACK, UNPACK and INSTRN groups). The
runtime reads the Tensix counters on every core after each compute kernel and
appends one `TS_DATA` row per counter to the device CSV; `worker/perf_counters.py`
decodes them by position (group order of the mask, table order of the bundled
`hw_counters.h`), divides by the counter window and averages over cores and
samples. The report stores `cases[].counters` (all counters, per-unit busy
fractions, per-thread stall fractions and reasons); the raw CSV is published as
`case-N-counters.csv`. Counter values describe the compute kernel window of each
core, not the ranked device time. Correctness is checked inside
the wrapper against a PyTorch reference with the manifest's `tolerance`
(`|got - want| <= atol + rtol * scale`); the broker only compares the verdict marker.
The wrapper's one-line correctness detail (mismatch count, max error) is appended to
the case message, and numeric correctness fields are copied into the public report.

Each pass warms the program cache in its own process. There is **no explicit
L1 or DRAM cache flush**. Separate passes can have different thermal, cache and
scheduling conditions. Profiling changes the workload and inter-sample spacing;
TT-Lang host samples are taken with the profiler enabled and therefore include
its overhead (profiler export itself happens outside the timed call).

### TT-Lang: two containers per submission

Since round 4 a TT-Lang submission is judged in two containers, not three per
testcase. The `profile` container opens the chip once and, for every testcase in
order, uploads the inputs, runs the operation once (correctness against the
PyTorch reference, first-call latency including JIT), then 3 warmups and 7 timed
samples whose device cycles are captured by byte range from the profiler CSV. A
wrong result is never timed; the batch stops at the first case that raises,
because the device state is not trusted afterwards. The wrapper writes one CSV
per case (`/tmp/tt-oj-cases/case-N-profile.csv`: the two header lines plus that
case's rows, byte ranges rebased), which the broker collects before the
container exits, so `case-N-device.csv` and its parser are unchanged. The
`counters` container repeats this for the accepted cases only with
`TT_METAL_PROFILE_PERF_COUNTERS` (1 warmup, 3 samples); the counters are
compiled into the kernels, so they cannot share a process with the ranked
timing. The output limit is 512 KiB per testcase for the whole container (the kernel
compiler's notes alone are about 120 KB per softmax testcase). Per-case wall limits apply inside the batch: the broker watches the
`TT_OJ_CASE_START` markers and ends the container with TLE when a case runs
longer than the limit; CSVs of finished cases are salvaged first. Container
start, `import ttnn` and device initialisation (about 6 s) are therefore paid
twice per submission instead of thirty times.


Raw host values are stored in nanoseconds. Device CSVs retain raw counter cycles;
the parser derives nanoseconds as `cycles * 1000 / recorded_MHz`. Recorded MHz is
not proof of a constant frequency throughout a kernel. Kernel waits and data
movement may contribute to the duration.

The parser pairs markers within a device/core/RISC/run identity. The same-core
envelope takes the first start and last finish on that core for a local stage.
**Do not sum parallel RISCs or cores.** There is no calibrated cross-core or
cross-device total elapsed time. `program_index` is an ordered occurrence within
a sample on a core/RISC/zone, not a global backend program ID.

The normal OJ time remains the sum of host-pass container wall times. It includes
startup, initialization, JIT and transfers and is distinct from either benchmark
metric. The separate device pass has its own execution metadata in the restricted
archive. Memory is sampled host container memory, not accelerator memory.

## Correctness and repeatability

The trusted manifest supplies inputs, expected JSON outputs, absolute tolerance
`atol` and relative tolerance `rtol`. Tolerances must be finite and nonnegative;
both default to zero. The broker recursively checks shape, rejects nonfinite
numeric results and uses `math.isclose` for numeric elements. Source code does
not choose the reference output or broker tolerances.

The report records input/reference/actual hashes, tolerances and comparison
results separately for host and device passes. Exact reference values stay in
the restricted manifest snapshot. Custom self-tests without an expected output
are explicitly marked `self_test_no_reference` and are not benchmark correctness
results.

Current wrappers check the first and final result across repeated execution;
**they do not read back and check every timed repeat**. TTNN checks first/final
equality, while the Metalium hosts check first/final outputs against their fixed
reference computations. The report retains the wrapper's repeatability flag and
labels this scope. A successful device diagnostic also requires broker reference
comparison and a validated CSV selection.

Submitted Python executes in the wrapper's process. A determined submission can
modify APIs, timing functions or result markers. Kernel code can also manipulate
its device environment. Neither wrapper authenticity nor anti-cheat attestation
has been established; the platform currently assumes trusted account holders.

## Scheduling and environment provenance

The host has four Blackhole chips (`/dev/tenstorrent/0-3`, two p300 boards).
`TT_OJ_CHIPS` (default `0,1,2`) lists the chips the judge may use; chip 3 is
left to interactive users of the machine, always the same one. The worker
requests `concurrency: <pool size>` from Hydro and keeps a chip pool: a
TT-Lang job takes one free chip for its whole lifetime, so up to three
submissions are judged at the same time, each on its own chip; the ttnn and
Metalium runtimes discover the whole mesh and therefore take every pooled chip
and run alone (they need `TT_OJ_CHIPS=0,1,2,3` to see a supported topology). Inside a TT-Lang container the reserved chip is mapped to
`/dev/tenstorrent/0` and `TT_MESH_GRAPH_DESC_PATH` names the bundled one-chip
(p150) mesh descriptor, because tt-metal treats a lone chip as a "CUSTOM"
cluster and refuses to start without one. Within a job, the testcases and the two
containers still run sequentially. The report's `scheduler.device_ids` records the
chip; chips share the host CPU set and PCIe root, which can perturb host-side
timings but not the on-device kernel cycles that are ranked. A process-lifetime
advisory `flock` still prevents two workers from starting on one host.

If the site process restarts while jobs are running, the Hydro task documents of
those jobs are lost and the records stay "Judging"; rejudge them (record page,
"Rejudge"). A worker restart or a dropped judge websocket is handled: Hydro
re-queues the in-flight tasks and they are judged again from the start.

Preflight excludes known external Docker containers exposing Tenstorrent devices
and waits if a recorded Blackhole temperature reaches 85 °C, for at most 120 s.
It does not exclude arbitrary host processes. Benchmark containers use the
deployment's recorded CPU set (currently `4-7,12-15`); OJ services use the other
set. CPU affinity does not reserve CPUs from OS tasks or interrupts.

For each attempt the worker resolves runtime tags to Docker image IDs and uses
those **IDs** for every container in that attempt. The report records runtime
tags, resolved IDs, repository digests where available, worker image ID when
available, SDK build/commit, driver/firmware/device clock observations, CPU
governors, temperatures and load snapshots. It also hashes the broker, metrics,
device parser, protocol, TTNN wrapper and packaged Metalium reference sources.
Runtime binary identity comes from the pinned image ID; source-to-binary
reproducibility is not independently attested.

Containers have no network, run as UID/GID 1005, use a read-only root, drop all
capabilities and cannot access the Docker socket. The per-case bind mount has
only the submitted source, current input and the required Python wrapper, with
no expected outputs. Limits are 8 GiB host memory, no extra swap, 512 processes,
2 GiB writable `/tmp`, combined stdout/stderr capture of 256 KiB, and device CSV
capture of 64 MiB. Each pass's wall timeout includes all initialization, JIT and
transfers and is capped at 180 s.

## Attempt identity and lifecycle

Every valid 24-character hexadecimal record ID received by the broker creates a
new UUID attempt ID. A rejudge of the same Hydro record therefore preserves its
previous attempt rather than overwriting it.

Before manifest download or syntax checking, the worker snapshots the exact
submitted text and minimal task metadata and writes an initial report. It then
checkpoints after manifest load, image resolution, case start, host completion
and case completion. Each checkpoint uses same-directory atomic replacement and
`fsync`; partially written JSON is not served.

`events.jsonl` records monotonically numbered events with UTC `utc` and host
`monotonic_ns`. Events include receipt, manifest loading, syntax checks, image
resolution, preflight, case/pass preparation, container creation/start/exit,
pass/case completion, stop requests and attempt termination. These are broker
observations, not precise hardware dispatch events. `lifecycle.queue_wait_ms` is
`null` because Hydro does not supply its enqueue timestamp. Monotonic timestamps
must not be compared across hosts or assumed comparable across reboots.

Normal AC, WA, CE, RE, SE, TLE, MLE and OLE outcomes all finalize a report and
inventory. This includes syntax errors, manifest fetch failures and preflight
failures before a first case. Cancellation finalizes a `CANCELLED` attempt and
marks unfinished cases interrupted. Containers are removed even if raw-output
archiving fails; a failed stop journal cannot prevent termination.

At startup the exclusive worker removes its own stale containers and finalizes
existing schema-2 `running` archives as interrupted. Recovery records the time
it **observed** the interruption; it does not invent the actual stop time or a
completed measurement. Truncated event appends are preserved separately while
valid events are recovered. Recovering an older attempt does not replace a
newer latest report. Abrupt power loss can still lose in-memory process output
before pass completion. A full disk or unavailable filesystem can prevent
persistence; no protocol can claim a successful archive in that condition.

## Storage and visibility

```text
data/jobs/<record_id>/attempts/<attempt_id>/     restricted archive
  task.json                                  provided uid only; no invented identity
  submission.py OR solution.cpp               exact submission
  policy.json                                 exact pinned policy
  manifest.json                               exact fetched bytes; includes references
  events.jsonl                                broker lifecycle
  case-N-host.stdout / .stderr / .json         bounded raw bytes and execution metadata
  case-N-device.stdout / .stderr / .json       separate pass, when executed (ttnn/Metalium)
  case-0-profile.* / case-0-counters.*         TT-Lang batch containers (all cases)
  N/ and N-device/                            per-pass bind mount contents
  case-N-device.csv                           raw profiler CSV, when captured
  terminal-details.json / hydro-cases.json     full judge diagnostics, when produced
  report.json / result.json                    finalized outcome
  inventory.json                              SHA-256 and byte size per artifact

data/profiles/<record_id>/attempts/<attempt_id>/
  report.json                                 safe metrics and outcome; no identity/source/logs
  case-N-device.csv                           captured raw profiler CSVs only

data/profiles/<record_id>/
  report.json / case-N-device.csv              compatible latest copies
  latest.json                                 latest attempt identity and state
```

The new private `attempts` subtree and attempt directories are mode 0700.
Temporary sandbox case directories/files are readable inside their specific
Docker mount. After finalization private files become mode 0400 and case
directories become mode 0700; public attempt artifacts become mode 0444. Site
containers mount only `profiles`, not `jobs`.

Profile download endpoints use native record visibility and only whitelist
`report.json` and `case-N-device.csv` from the latest record directory. Reports
therefore exclude participant IDs, source text, input/reference values, raw
stdout/stderr, compiler text and arbitrary infrastructure error messages. Full
diagnostics stay in the restricted archive and normal judge permission flow.
These profile downloads are not automatically a publicly licensed research
dataset. Raw profiler CSVs may contain source paths or user zone metadata.

On the site, a record's details are visible to its **owner or staff** only. A
viewer is owner-or-staff when `user._id === rdoc.uid` or the account holds
`PRIV_READ_RECORD_CODE` (the super admin and the instructor account). For
everyone else the record page (`/record/:rid`, including the websocket
re-render of the status block) shows only the overall verdict, the verdict and
device time per testcase, the device-time geometric mean and the leaderboard
rank; judge messages, compiler text, the judge log, hardware counters, timing
details and the source code are hidden, and `/record/:rid/profile` and
`/record/:rid/profile/:artifact` answer 403. The leaderboard itself is public
to logged-in accounts, so rank, speedup and per-testcase device times are not
considered private.

The finalized inventory lists exact private/public file paths, SHA-256 and byte
sizes. It excludes itself to avoid a self-hash cycle. It does not inventory
mutable compatibility copies. Archives are read-only by convention, but root or
an administrator can change them. Checksums detect changes against the inventory;
they are not signatures, remote attestations or write-once storage.

Retention is currently **indefinite until manual administrator removal**. There
is no automatic deletion, source publication, user-to-user source export or
backfill. Back up both `jobs` and `profiles` together. Space monitoring, backup
restore tests and a formal retention/participant consent policy remain separate
operational work before a public data collection campaign.

## Auditing and metrics export

`tools/research_data.py` verifies archive identity, exact inventory coverage,
checksums, source/manifest/policy fingerprints, public/private report equality,
measurement counts and receipt-to-terminal lifecycle sequencing. Use privileged
local access because restricted archives belong to the worker's root user:

```bash
sudo -n python3 tools/research_data.py audit \
  --output data/protocol-audit.json

sudo -n python3 tools/research_data.py export \
  --key-file data/research-pseudonym.key \
  --output data/research-metrics.jsonl
```

Output paths must not already exist. `--records <rid> ...` selects records; all
preserved finalized attempts for selected records are included. Unfinished
attempts are skipped. Legacy schema-1 reports cannot be converted into schema-2
archives by inventing missing data.

The exporter substitutes keyed HMAC pseudonyms for record, attempt and supplied
participant identities. A missing participant UID stays missing. It excludes
source, inputs, answers, logs, usernames, emails and exact timestamps, and exports
only selected numeric metrics and provenance. Zone/RISC strings are pseudonymized
and unknown free-text reasons are redacted. The private key is created mode 0600;
keeping it enables linkage across exports and losing it prevents that linkage.
Pseudonymization is not anonymization: review the data and obtain the necessary
participant authorization before publishing it. Source-level research needs a
separate explicit consent and access plan; this tool does not release code.

## Repeated experiments

`tools/run_experiment.py` submits each implementation at least three times through
the same OJ queue. With both reference and candidate, order is randomized within
each repeat using a recorded seed. Each submission has fresh case containers and
its own attempt archive. The local experiment file records source hashes, order,
record/attempt results and aggregate within-submission medians.

```bash
python3 tools/run_experiment.py \
  --problem TT1001 --language ttnn \
  --reference problems/ttnn-add/accepted.py \
  --candidate /absolute/path/to/candidate.py \
  --repeats 3 --seed 0 --label add-repeatability
```

Use an actual reference source path present in the repository. The tool groups
matching protocol, manifest, runtime images, wrapper/broker code hashes,
driver/firmware, CPU governors and CPU affinity. It reports host nanoseconds and
device same-core envelope cycles separately, along with variation between fresh
submissions. It retains all samples. Fresh submissions do not prove statistical
independence or remove time drift, thermals and shared-host interference.

## Remaining limits before research performance claims

- CPU/IRQ isolation and arbitrary external workload exclusion are not verified.
- Device frequency is observed, not locked or fully calibrated.
- Cross-core total device elapsed time has not been calibrated.
- Instrumented device and uninstrumented host measurements are different passes.
- First/final checks do not prove every repeat is correct; wrappers are not tamper-attested.
- Consecutive samples can be correlated. The deterministic bootstrap median
  interval assumes IID samples and is diagnostic, not an accuracy guarantee.
- Benchmark coverage, representative workloads, numerical tolerances and
  cross-implementation baselines still need task-specific expert review.

The report's `quality.reason_codes` makes these limits machine-readable; its
`eligible_for_ranking` remains false even when a case passes and appears stable.
