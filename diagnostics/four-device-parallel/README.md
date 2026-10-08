# Four-device execution overlap

This diagnostic uses one trusted Metalium process to create four unit meshes
with `MeshDevice::create_unit_meshes({0, 1, 2, 3})`. Each has its own workload,
command queue, DRAM output and L1 scratch buffer.

A bounded BRISC kernel waits for 337,500,000 device clock cycles (nominally
250 ms at 1350 MHz), adds two device-specific numbers, and writes a unique
tag, sum and cycle interval to DRAM. The host checks every output. One Tensix
core is used on each ASIC. This demonstrates simultaneous kernel execution,
not realistic tensor computation throughput or independent process isolation.

After JIT warmup, three comparisons gave:

| Round | Submit and finish each device in turn | Submit all four, then finish all | Ratio |
| --- | ---: | ---: | ---: |
| 1 | 1000.096975 ms | 250.109371 ms | 3.998639 |
| 2 | 1000.114298 ms | 250.058724 ms | 3.999518 |
| 3 | 1000.071386 ms | 250.165869 ms | 3.997633 |

`result.json` contains the raw measurements and verified outputs. Profiling
was disabled. Input preparation, JIT and output downloads are outside these
host intervals. Host intervals include enqueue and synchronization overhead;
timestamps from different ASICs are not compared.

`run.sh` briefly stops the OJ worker, takes the existing hardware lock, runs
the diagnostic in a readonly, network-free container, and restores the worker
on exit. The OJ scheduler itself remains unchanged and serial. The runtime
and source are the same pinned version as the OJ profiler toolchain.

This test does **not** show that four existing OJ containers can independently
initialize the whole mesh, that real workloads achieve 4× speedup, or that
parallel benchmarking has no timing interference. Those require separate
device ownership, process isolation and interference measurements.
