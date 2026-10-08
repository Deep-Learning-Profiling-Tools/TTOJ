"""Trusted TTNN task wrapper. Only submission.py and this wrapper reach the job."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

import numpy as np
import ttnn


def main():
    data = json.load(sys.stdin)
    spec = importlib.util.spec_from_file_location("submission", "/workspace/submission.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "solve", None)):
        raise TypeError("Please define solve(a, b) and return a device TTNN Tensor")
    device = ttnn.open_device(device_id=0)
    try:
        device.enable_program_cache()
        a = ttnn.Tensor(np.asarray(data["a"], dtype=np.float32), ttnn.bfloat16, device, ttnn.TILE_LAYOUT)
        b = ttnn.Tensor(np.asarray(data["b"], dtype=np.float32), ttnn.bfloat16, device, ttnn.TILE_LAYOUT)
        ttnn.synchronize_device(device)
        first_start = time.perf_counter_ns()
        result = module.solve(a, b)
        ttnn.synchronize_device(device)
        first_call_ns = time.perf_counter_ns() - first_start
        if not isinstance(result, ttnn.Tensor):
            raise TypeError("solve() must return a TTNN Tensor")
        if not result.is_allocated() or result.storage_type() != ttnn.StorageType.DEVICE:
            raise TypeError("solve() must return a tensor on the Tenstorrent device")
        host = ttnn.to_layout(ttnn.from_device(result), ttnn.ROW_MAJOR_LAYOUT)
        host = ttnn.to_dtype(host, ttnn.float32)
        values = np.asarray(host.to_numpy(), dtype=np.float32)
        first_values = values.copy()
        profile = None
        if os.getenv("TT_OJ_PROFILE_ONLY") == "1":
            csv_path = Path("/tmp/generated/profiler/.logs/profile_log_device.csv")
            flush = lambda: ttnn._ttnn.device.ReadDeviceProfiler(device)
            flush()  # export the first invocation separately from warmed samples
            for _ in range(5):
                result = None
                result = module.solve(a, b)
                ttnn.synchronize_device(device)
                flush()
            ranges = []
            for sample in range(1, 11):
                result = None
                before = csv_path.stat().st_size if csv_path.exists() else 0
                result = module.solve(a, b)
                ttnn.synchronize_device(device)
                flush()  # exporting is outside the device kernel cycle interval
                after = csv_path.stat().st_size if csv_path.exists() else 0
                ranges.append({"sample": sample, "start_byte": before, "end_byte": after})
            host = ttnn.to_layout(ttnn.from_device(result), ttnn.ROW_MAJOR_LAYOUT)
            host = ttnn.to_dtype(host, ttnn.float32)
            stable = bool(np.array_equal(first_values, np.asarray(host.to_numpy(), dtype=np.float32)))
            profile = {"schema_version": 1, "timing_scope": "device_kernel_cycles",
                "instrumented": True, "repeatability_verified": stable,
                "device_capture": {"warmup": 5, "sample_count": 10,
                    "selection": "csv_append_byte_ranges", "sample_byte_ranges": ranges,
                    "export_policy": "Profiler flushed after every invocation; export overhead excluded from kernel cycles but changes inter-sample spacing"}}
            if not stable:
                raise RuntimeError("Results changed during the instrumented device pass")
        if os.getenv("TT_OJ_BENCHMARK") == "1":
            warmups = int(os.getenv("TT_OJ_WARMUP", "5"))
            count = int(os.getenv("TT_OJ_SAMPLES", "50"))
            if not 1 <= warmups <= 100 or not 5 <= count <= 1000:
                raise ValueError("Invalid benchmark policy")
            samples = []
            for _ in range(warmups):
                result = None
                result = module.solve(a, b)
                ttnn.synchronize_device(device)
            ttnn.synchronize_device(device)
            measurement_start_ns = time.perf_counter_ns()
            for _ in range(count):
                result = None  # release the previous output outside this sample
                start = time.perf_counter_ns()
                result = module.solve(a, b)
                ttnn.synchronize_device(device)
                samples.append(time.perf_counter_ns() - start)
            measurement_end_ns = time.perf_counter_ns()
            host = ttnn.to_layout(ttnn.from_device(result), ttnn.ROW_MAJOR_LAYOUT)
            host = ttnn.to_dtype(host, ttnn.float32)
            last_values = np.asarray(host.to_numpy(), dtype=np.float32)
            stable = first_values.shape == last_values.shape and bool(np.array_equal(first_values, last_values))
            profile = {"schema_version": 1, "timing_scope": "host_submit_to_device_complete",
                "scope_detail": "Python solve(a,b) plus device synchronization; input transfer and output readback excluded; allocations inside solve included",
                "clock": "perf_counter", "clock_resolution_ns": round(time.get_clock_info("perf_counter").resolution * 1e9),
                "warmup": warmups, "samples_ns": samples, "first_call_including_jit_ns": first_call_ns,
                "measurement_start_ns": measurement_start_ns, "measurement_end_ns": measurement_end_ns,
                "repeatability_verified": stable,
                "device_profiler": {"available": os.getenv("TT_OJ_DEVICE_PROFILER_SUPPORTED") == "1"}}
            if not stable:
                raise RuntimeError("Results changed after repeated benchmark runs")
        print("TT_OJ_RESULT:" + json.dumps(values.tolist(), allow_nan=False), flush=True)
        if profile:
            print("TT_OJ_PROFILE:" + json.dumps(profile, allow_nan=False), flush=True)
    finally:
        ttnn.close_device(device)


if __name__ == "__main__":
    main()
