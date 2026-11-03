# CS540 Tenstorrent online judge: student guide

The course's online judge (OJ) compiles your TT-Lang operation, runs it on one Tenstorrent Blackhole chip, checks the result against a PyTorch reference and measures how long the kernel takes on the device. It hosts the three exercises TT3001 vector addition, TT3002 ragged row sum and TT3003 ragged softmax.

## Getting in

The OJ address is http://98.93.3.136:28888/ (plain HTTP, no `https`). It is served over plain HTTP from a university-hosted machine, so your password travels unencrypted: use a password that you do not use anywhere else.

Accounts are created by the instructor; there is no self-registration. Log in at `/login` with the username and password you were given. After the first login, change the password at `/home/security` (also under your username in the menu). The site first asks you to confirm your current password ("Confirm Access"), then shows the change-password form.

The navigation bar has **Accelerator problems** (the problem list), **Leaderboard**, **Submissions** (all records, filterable by problem, user and status), **Profiling** (how the measurements are made) and a light/dark toggle.

## The three problems

Each problem accepts one Python file that defines one `@ttl.operation`. The judge imports your file and calls that operation exactly as in the signature, nothing else. Inputs are already FP32 tile-layout tensors in device DRAM; `out` is preallocated with zeros. Do not open or close the device, do not read stdin, do not print the answer.

| Problem | Signature | Task |
| --- | --- | --- |
| TT3001 | `vector_add(a, b, out)` | `out = a + b`, N up to 16,777,217. `a`, `b`, `out` are padded to `(P/32, 32)` tiles with `P = 1024 * ceil(N/1024)`; only the first N values are checked. |
| TT3002 | `ragged_sum(x, valid, out)` | `out[r] = sum(x[r, :lengths[r]])`. `out` is `(R_pad, 32)`; column 0 of the first R rows is read. |
| TT3003 | `ragged_softmax(x, valid, out)` | Softmax of each valid prefix; positions outside the prefix and whole empty rows are 0. `out` is `(R_pad, W_pad)`; the top-left `R x L` block is checked. |

The ragged problems do not pass `lengths`. They pass `valid`, an FP32 tensor with the same padded shape as `x` holding 1.0 on the first `lengths[r]` columns of row r and 0.0 everywhere else, padding included. Rows can be up to 16,384 wide (512 tiles), which does not fit in L1 as one block, so wide rows must be processed in chunks. For softmax, subtract the row maximum before exponentiating and guard the empty-row denominator with `ttl.max(denominator, eps)`.

Correctness is `|got - want| <= atol + rtol * scale` per element: `atol = 1e-6, rtol = 2^-8` for TT3001 (scale `|a| + |b|`) and TT3002 (scale: sum of `|x|` over the valid prefix), and `atol = 1e-4, rtol = 2^-6` for TT3003 (scale: the reference value). The tolerance is wider than FP32 epsilon because the FPU rounds operands to about 11 significant bits. Each problem has 10 testcases generated from a fixed seed; the statement lists their shapes.

**What the problem page shows.** The statement is on the left. On the right is a code editor preloaded with the starter code (also attached as `starter.py` under the **Files** tab), with the language preset to "Tenstorrent TT-Lang (Python)". Your previous submissions to this problem are listed below the editor. If the split view does not open, click **Open Scratchpad (Alt+E)**. The classic **Submit** page (`/p/TT3001/submit`) also works: paste the code or upload a file.

The starters are single-core baselines. The TT3001 and TT3003 starters are correct but slow. The TT3002 starter asserts `W_pad <= 256` and fails the wide testcases until you add chunking.

## Submitting and the statuses

Press **Submit** in the editor. There is no pretest or "Run" step for TT-Lang: every submission runs the official testcases and counts against your quota.

The judge runs your file on one Blackhole chip in a container that goes through the 10 testcases in order: for each one it runs your operation once and checks the result, and if it is correct it runs 3 warmups and 7 timed repeats with the device profiler on. A second container then reads the hardware counters for the accepted testcases. An accepted solution takes about a minute; a wrong one less. The judge uses three of the machine's four chips, so up to three submissions from the class are judged at the same time; further submissions wait in a queue. The record page updates itself while judging runs (progress percentage and judge-log lines such as "Testcase 3 / 10 finished").

| Status | Meaning | Typical cause |
| --- | --- | --- |
| Accepted (AC) | Every testcase within tolerance. | |
| Wrong Answer (WA) | An element outside tolerance, wrong output shape, or NaN/Inf. | Padding mishandled, mask missing, masked entries leaking into a sum, overflow in `exp`. |
| Compile Error (CE) | The file could not be compiled. | A Python syntax error, or a `TTLangCompileError` from the TT-Lang compiler. |
| Runtime Error (RE) | A Python exception while running, or no result produced. | `AssertionError`, a device error, a transaction that never completes, or a missing operation name. |
| Time Exceeded (TLE) | A pass exceeded the 180 s wall limit (container start, device initialisation, compilation and all timed calls included). | A kernel that is extremely slow or hangs. |
| Memory Exceeded / Output Exceeded | More than 8 GiB host memory, or more than 256 KiB of stdout/stderr. | Printing in a loop. |

A submission with some failed testcases gets a partial score and the first failing status; only fully accepted submissions are timed on every case and ranked.

## Reading the record page

Open a submission from the problem page, the **Submissions** list or the leaderboard.

**Status headline.** The overall status, the score, "n / 10 testcases accepted" and the problem. For CE, the compiler output appears here.

**Judge messages per testcase.** One row per testcase with the result, a message and the container wall time. For accepted and wrong cases the message gives the shape, how many values were outside tolerance, the maximum absolute error and the worst error/bound ratio (above 1 means a mismatch). For RE the message is the tail of the log with the Python traceback. The wall time (typically 1 to 3 s) is that testcase's share of the container run, including compilation, not kernel time.

**Performance tiles.** *Device time (geomean)*, the ranked number, with the speedup over the single-core reference; *Leaderboard* rank; *Cores used* (maximum over testcases, of 110 available); *FPU / SFPU busy* and *Unpack / Pack busy* averaged over testcases; *Submissions used*. Below the tiles are **Download report JSON** (the full report with raw samples) and a link to the profiling explanation.

**Testcases table.** Per testcase: result, device time, cores, bandwidth, FPU, SFPU, Unpack and Pack busy fractions, Stalled fraction and host median. *Bandwidth* is the bytes the problem must at least move (every padded input read once, the output written once) divided by device time; Blackhole DRAM peaks at about 512 GB/s, so this shows how far you are from memory-bound. *Host median* is the median of the 7 timed calls including dispatch and synchronisation, measured with the profiler on; it is diagnostic only.

**Hardware counters.** One expandable block per testcase: cores, counter window, each unit's busy fraction (mean over cores and busiest core), the three instruction threads with stall fraction, instructions per cycle and top stall reasons, a table of all 85 raw counters, and a raw-counter CSV download. See the next section.

**Device timing details.** Per testcase: the device clock and a table of per-core, per-RISC kernel zones with sample count, median and P95 microseconds and median cycles. BRISC and NCRISC run the data-movement kernels (reader, writer); TRISC0/1/2 run unpack, math and pack. These stages overlap, so do not add them up. A device CSV download and the host-side timing summary are at the bottom of each block.

## What the hardware counters mean

Counters are read in a second container (1 warmup, 3 samples per accepted testcase) with the Tensix counter groups FPU, PACK, UNPACK and INSTRN enabled. Every value is a fraction of the *window*, the compute kernel's run on that core from start to end, averaged over cores and samples. The window is not the device time, which also spans the reader and writer kernels. Counters never change the ranking.

- **FPU (matrix unit) busy**: fraction of the window the FPU was executing. Tile adds, multiplies, reductions and broadcasts run here.
- **SFPU (vector unit) busy**: the same for the SFPU, which runs `ttl.exp`, `ttl.recip`, `ttl.max` and other per-element functions. Softmax kernels are typically SFPU-heavy.
- **Math pipeline busy**: any math instruction active (FPU or SFPU).
- **Unpacker 0 / 1 busy**: fraction of the window the unpackers were moving tiles from L1 into the SrcA/SrcB registers. The Testcases table shows the busier of the two.
- **Packer busy**: fraction of the window the packer was writing results from DST back to L1.
- **Threads**: each Tensix core has three instruction threads, unpack (TRISC0), math (TRISC1) and pack (TRISC2). *Stalled* is the fraction of window cycles the thread could not issue; *instructions / cycle* is its issue rate. The *Stalled* column in the Testcases table is the fraction of the window in which at least one thread was stalled.
- **Stall reasons** (per thread, largest first): *waiting on semaphore (data not ready)*: the thread waited for a dataflow buffer to be filled, usually by the reader; *waiting on semaphore (buffer full)*: it waited for the consumer to free space; *waiting for SrcA/SrcB valid*: math waited for the unpacker; *waiting for SrcA/SrcB clear*: the unpacker waited for math to consume the registers; *waiting for unpack/pack/math/SFPU idle*: synchronisation waits for another unit to finish.
- **Mean vs busiest core**: with a multi-core grid, a low mean and a high maximum means the work is split unevenly.

For the lab report, quote the device time as the result and use the counters as the explanation. Low FPU/unpack/pack busy together with a large "data not ready" stall and a bandwidth far below 512 GB/s points at data movement as the bottleneck. High SFPU busy with few stalls points at compute. One core at 100% and a low mean points at load imbalance. The per-RISC tables and raw CSVs allow deeper analysis, but never sum durations of parallel stages or cores.

## Leaderboard and speedup

`/leaderboard` has one table per problem. Each account appears once with its fastest accepted submission, ranked by **device time**: per testcase, the time from the first kernel start to the last kernel end over all cores, median of 7 instrumented samples; then the geometric mean over the 10 testcases. Host times and wall times are never ranked. *Speedup* is the single-core reference geometric mean divided by yours (about 22.1 µs for TT3001, 68.5 µs for TT3002 and 1100 µs for TT3003); "per testcase" expands the per-case times and speedups. The page does not refresh by itself; use the **Refresh now** link. A submission appears only when every testcase is accepted and has device timing.

## The submission limit and tips

You may submit at most **200 times per problem**. Compile errors, wrong answers and runtime errors all count; a rejudge by the staff does not. The problem page and your record pages show how many you have used. When the limit is reached, submitting fails with a message to ask the instructor.

- Test locally before submitting: run your file in the course TT-Lang environment with the padded shapes from the statement, including N = 1, the tile-boundary cases (1023, 1024, 1025) and the widest ragged rows. The OJ runs tt-lang `1.1.10.dev20260921`, with `ttl.grid_size(dims=1)` and `ttl.node(dims=1)` available.
- Get correctness first, then speed. A fast kernel that fails one case has no device time, and a testcase that raises stops the remaining ones.
- Do not resubmit while a submission is still queued, and do not wait until the deadline: with the whole class submitting, a few minutes per submission adds up.

## Privacy

Your source code is visible only to you and the instructor. Other students can see your device times on the leaderboard and on your record page, but not your code, judge messages, compiler output, hardware counters or the downloadable report.

## FAQ

**My submission has said "Waiting" for a long time.** All three judge chips are busy with submissions ahead of yours; each one takes about a minute. Do not resubmit.

**Why is the device time empty ("needs every testcase accepted")?** The device and counter passes run only for accepted testcases, and the geometric mean needs all 10.

**Why does the host median differ so much from the device time?** The host median includes Python dispatch, program launch and synchronisation, about 140 µs even for a trivial kernel; the device time is only the kernel's execution.

**Can I get a submission back?** No. If you believe the judge itself failed (System Error, or a device problem in the log), tell the instructor; a rejudge does not count.

**Where is the measurement methodology described?** On the **Profiling** page (`/profiling`) and in the "Profile summary" section of each problem statement.
