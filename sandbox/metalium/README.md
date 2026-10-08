# Metalium device-kernel judge

This image runs a trusted C++ host harness and JIT compiles the submitted device kernel from `/workspace/solution.cpp`. The submission cannot replace the host executable. The harness reads `a b` from stdin, uses logical Device 0 and Tensix core `(0, 0)`, reads the actual device output buffer back to host, closes the device, and emits one result marker:

- `/opt/tt-oj/bin/judge-riscv`: `TT_OJ_RESULT:<uint32 JSON number>`.
- `/opt/tt-oj/bin/judge-compute`: `TT_OJ_RESULT:<JSON array of all 1024 BF16 output values>`.

Other stdout contains Metalium logs and must be ignored. The worker should extract the single marker and compare its parsed JSON against the trusted test output. Compilation and execution failures return nonzero with diagnostics on stderr; cold firmware/kernel JIT is included in the container wall time. The two problem packages use 120-second per-case limits.

## Build

```sh
docker build -t tt-oj-metalium:0.79.0 sandbox/metalium
```

SDK packages are pinned to TT-Metalium 0.79.0 for Ubuntu 24.04 and SFPI 7.83.0, from Tenstorrent's official signed apt repository. `tt-pkg-key.asc` is the already installed repository signing key. The three MPI shared libraries under `mpi/` were copied from the official Tenstorrent Metalium runtime image already used by the local kernel demos. They supply the ULFM `MPIX_Comm_revoke` and `MPIX_Comm_shrink` symbols required by packaged `libtt_metal.so`; `LD_LIBRARY_PATH` scopes them to this container.

The host sources are adapted from the Apache-2.0 Tenstorrent `add_2_integers_in_riscv` and `add_2_integers_in_compute` examples installed by `tt-metalium-examples`. Compute reader and writer kernels remain the installed official kernels. The contestant only supplies the selected compute or data-movement kernel.

## Container requirements

Use numeric UID/GID `1005:1005`, no network, a read-only root filesystem, dropped capabilities and `no-new-privileges`. Mount the submitted source read-only at `/workspace/solution.cpp`. Provide writable `/tmp` for HOME, firmware and kernel caches, runtime logs and JIT intermediate files. Provide all four `/dev/tenstorrent` device nodes for topology discovery. Only logical Device 0 is allocated for the program. The current tests pass without mounting the host hugepage filesystem into the container.

The worker must serialize access to the shared accelerator devices and terminate the whole container on timeout. Device-level kernel isolation and recovery after a hung kernel require further platform work; these containers are intended for authenticated internal experiments.

## Problems

`problems/metalium-riscv/metadata.json` stores five uint32 cases including modulo-2^32 overflow. `problems/metalium-compute/metadata.json` stores five full-tile BF16 cases, with integer, negative and fractional values whose sums are exactly representable. Both directories include an accepted official kernel and an intentional compile-error example. The RISC-V directory also includes a subtraction kernel for Wrong Answer checks.

## Validation on this machine

The accepted RISC-V kernel returned 21 for `14 7`. The accepted compute kernel returned exactly 1024 elements, all equal to 21, under the production worker flags (UID 1005, no network, read-only root, all capabilities dropped, no-new-privileges, 8 GiB memory, 4 CPU quota and writable 2 GiB `/tmp`). No host hugepage mount was used. Cold container execution for the compute example took approximately 5 seconds. The intentional `#error` submission exits nonzero; compiler diagnostics contain the failing source path. Logs are `riscv-test.log`, `compute-test.log` and `compile-error-test.log`.

## Profiling

The profiling harness image is `tt-oj-metalium:0.79.0-profile1`. Set `TT_OJ_BENCHMARK=1` to collect 50 measurements after the initial correctness/JIT invocation and five additional warmup invocations. Each measurement starts immediately before `EnqueueMeshWorkload` and ends after `distributed::Finish`. Input upload, output download, device initialization, JIT and container startup are excluded. Queue submission, dispatch, kernel execution and completion wait are included. First and final device outputs are checked independently by the trusted host. Measurements are emitted in a `TT_OJ_PROFILE:` JSON line, and the normal result marker remains unchanged.

Samples use `std::chrono::steady_clock` and are expressed as integer nanoseconds. Linux `clock_getres(CLOCK_MONOTONIC)` reports resolution, not accuracy. `measurement_start_ns` and `measurement_end_ns` use the steady-clock epoch, allowing the Linux worker to correlate the sample window with monotonic-clock telemetry. CPU affinity, scheduling noise, throttling and device clock stability still affect results; a single short kernel is typically dominated by host dispatch/completion overhead.

The packaged Metalium 0.79.0 release exports device-profiler APIs as no-op stubs. Static inspection shows `ReadMeshDeviceProfilerResults`, `ReadDeviceProfilerResults` and `InitDeviceProfiler` each consist of a single `ret` instruction. The [v0.79.0 runtime options](https://github.com/tenstorrent/tt-metal/blob/v0.79.0/tt_metal/llrt/rtoptions.cpp) explicitly reject `TT_METAL_DEVICE_PROFILER` without a Tracy-enabled build. Accordingly, this image reports `device_profiler.available=false`; it does not invent cycle counts.

The same host sources can be linked against a genuine Tracy-enabled SDK with CMake option `-DTT_OJ_DEVICE_PROFILER_SUPPORTED=ON`. In that image, `TT_OJ_PROFILE_ONLY=1` retains the initial correctness/JIT invocation, runs five warmups and then captures ten instrumented repeats. The profiler is flushed after every invocation with `TT_METAL_PROFILER_MID_RUN_DUMP=1` to avoid retaining multiple runs in the limited marker buffer. The CSV remains at `/tmp/generated/profiler/.logs/profile_log_device.csv` and includes initial/warmup rows. `device_capture` selects each measured repeat by its appended CSV byte range; repeated programs may reuse a run host ID. The selection has `warmup:5`, `sample_count:10`, and `sample_byte_ranges:[{sample,start_byte,end_byte,run_host_ids,valid}]`. Flush and CSV export occur outside the device kernel intervals. This diagnostic pass has no host benchmark samples and its instrumentation changes execution timing. DPRINT and Watcher are disabled for both benchmark and diagnostic passes because they affect SRAM and timing. See the [official device-profiler guide](https://docs.tenstorrent.com/tt-metal/latest/tt-metalium/tools/device_program_profiler.html).

`worker/device_profile.py` parses the official CSV into per-RISC kernel intervals and per-core envelopes. Pass the `device_capture` object as its optional second argument to exclude initialization and warmups from `kernels`, `statistics` and `core_statistics`. Statistics include raw samples, count, minimum, median, interpolated p95, maximum, mean and standard deviation in cycles and frequency-derived nanoseconds. Each group uses `program_index`, the 1-based occurrence within a sample for a given core/RISC/zone, so two launches within one solve are summarized as separate ordered stages. This is a local stage order, not a global backend program identifier. The `complete` flag and `expected_sample_count` expose missing repeats. The parser preserves raw cycles, validates the full CSV, reports malformed/truncated/misordered captures and never sums parallel RISCs or infers a shared clock across devices. Without capture metadata it explicitly labels the result an unselected cold diagnostic. Invalid selection metadata returns no selected kernels. Parser fixtures are in `test_device_profile.py`.

The first hardware benchmark captured 50 samples with five warmups: RISC-V median 11,427 ns (minimum 10,820 ns, maximum 314,059 ns) and compute median 12,284 ns (minimum 10,209 ns, maximum 12,955 ns). Device clock was 1350 MHz before and after both runs. The RISC-V outlier demonstrates host scheduling noise. These are measurements of this tiny workload in one process, not hardware arithmetic latency or performance guarantees.
