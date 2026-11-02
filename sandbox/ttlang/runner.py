"""Trusted TT-Lang exercise wrapper for the CS540 problems.

Only submission.py and this wrapper reach the job. The wrapper owns input
generation, padding, device allocation, the reference computation and the
tolerance check; the submission only provides the TT-Lang operation.

stdin: one JSON object (the problem test case), for example
  {"exercise": "vector-add", "entry": "vector_add", "seed": 540, "case_id": 3,
   "N": 1025, "tolerance": {"atol": 1e-6, "rtol": 0.00390625}}
  {"exercise": "ragged-sum", "entry": "ragged_sum", "seed": 540, "case_id": 1,
   "R": 256, "L": 65, "tolerance": {"atol": 1e-6, "rtol": 0.00390625}}
  {"exercise": "ragged-softmax", "entry": "ragged_softmax", ...}

stdout markers (read by the judge worker):
  TT_OJ_RESULT:{"verdict": "pass"}            exact text on success; anything else is a wrong answer
  TT_OJ_DETAIL:<one line of human-readable correctness detail>
  TT_OJ_PROFILE:{...}                        host timing samples or device-capture metadata
"""
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import time

import torch
import ttnn

MARKER = "TT_OJ_RESULT:"
DETAIL = "TT_OJ_DETAIL:"
PROFILE = "TT_OJ_PROFILE:"
EXERCISES = {"vector-add": "vector_add", "ragged-sum": "ragged_sum", "ragged-softmax": "ragged_softmax"}
WORKSPACE = Path(os.getenv("TT_OJ_WORKSPACE", "/workspace"))


def ceil_to(value, multiple):
    return multiple * math.ceil(value / multiple)


def sha256_tensor(tensor):
    return hashlib.sha256(tensor.contiguous().numpy().tobytes()).hexdigest()


class Case:
    """Deterministic logical inputs, padded device layout and reference output."""

    def __init__(self, spec):
        self.exercise = spec["exercise"]
        if self.exercise not in EXERCISES:
            raise ValueError(f"Unknown exercise {self.exercise!r}")
        self.entry = spec.get("entry") or EXERCISES[self.exercise]
        tolerance = spec.get("tolerance") or {}
        self.atol = float(tolerance.get("atol", 0))
        self.rtol = float(tolerance.get("rtol", 0))
        if not (math.isfinite(self.atol) and math.isfinite(self.rtol) and self.atol >= 0 and self.rtol >= 0):
            raise ValueError("Tolerances must be finite and nonnegative")
        generator = torch.Generator().manual_seed(int(spec.get("seed", 540)) * 1000 + int(spec.get("case_id", 0)))
        if self.exercise == "vector-add":
            n = int(spec["N"])
            if n < 1:
                raise ValueError("N must be positive")
            self.logical = {"a": torch.randn(n, generator=generator), "b": torch.randn(n, generator=generator)}
            self.reference = self.logical["a"] + self.logical["b"]
            padded = ceil_to(n, 1024)
            rows = padded // 32
            self.device_shapes = {"a": (rows, 32), "b": (rows, 32), "out": (rows, 32)}
            self.padded = {name: self._pad_vector(self.logical[name], padded).reshape(rows, 32) for name in ("a", "b")}
            self.describe = f"N={n} (padded to {rows}x32 tiles)"
        else:
            r, l = int(spec["R"]), int(spec["L"])
            if r < 1 or l < 1:
                raise ValueError("R and L must be positive")
            lengths = torch.randint(0, l + 1, (r,), generator=generator)
            if self.exercise == "ragged-sum":
                x = torch.randn(r, l, generator=generator)
            else:
                x = torch.empty(r, l).uniform_(-80.0, 80.0, generator=generator)
            self.logical = {"x": x, "lengths": lengths}
            columns = torch.arange(l)
            valid = (columns[None, :] < lengths[:, None]).to(torch.float32)
            if self.exercise == "ragged-sum":
                self.reference = (x * valid).sum(dim=1)
            else:
                logits = x.masked_fill(valid == 0, -torch.inf)
                logits = torch.where(lengths[:, None] > 0, logits, torch.zeros_like(logits))
                self.reference = torch.softmax(logits, dim=1).masked_fill(valid == 0, 0.0)
            rows, width = ceil_to(r, 32), ceil_to(l, 32)
            x_padded = torch.zeros(rows, width)
            x_padded[:r, :l] = x
            valid_padded = torch.zeros(rows, width)
            valid_padded[:r, :l] = valid
            self.padded = {"x": x_padded, "valid": valid_padded}
            out_shape = (rows, 32) if self.exercise == "ragged-sum" else (rows, width)
            self.device_shapes = {"x": (rows, width), "valid": (rows, width), "out": out_shape}
            self.valid = valid
            self.describe = f"R={r} L={l} (padded to {rows}x{width}; lengths in [0, {l}])"
        self.input_sha256 = hashlib.sha256(b"".join(sha256_tensor(t).encode() for t in self.padded.values())).hexdigest()

    @staticmethod
    def _pad_vector(values, padded):
        return torch.cat([values, torch.zeros(padded - values.numel())])

    def crop(self, host_out):
        if self.exercise == "vector-add":
            return host_out.reshape(-1)[: self.reference.numel()]
        r = self.reference.shape[0]
        if self.exercise == "ragged-sum":
            return host_out[:r, 0]
        return host_out[:r, : self.reference.shape[1]]

    def check(self, result):
        """Elementwise |got - want| <= atol + rtol * scale, with an exercise-specific scale."""
        if tuple(result.shape) != tuple(self.reference.shape):
            return False, {"reason": "shape_mismatch"}, f"Output shape {tuple(result.shape)} differs from {tuple(self.reference.shape)}"
        if not torch.isfinite(result).all():
            return False, {"reason": "nonfinite_output"}, "Output contains NaN or infinite values"
        error = (result - self.reference).abs()
        if self.exercise == "vector-add":
            scale = self.logical["a"].abs() + self.logical["b"].abs()
        elif self.exercise == "ragged-sum":
            scale = (self.logical["x"].abs() * self.valid).sum(dim=1)
        else:
            scale = self.reference.abs()
        bound = self.atol + self.rtol * scale
        ratio = error / bound.clamp_min(1e-30)
        mismatches = int((error > bound).sum())
        detail = {"checked_count": int(error.numel()), "mismatch_count": mismatches,
                  "max_abs_err": float(error.max()), "max_err_to_bound_ratio": float(ratio.max()),
                  "atol": self.atol, "rtol": self.rtol, "output_sha256": sha256_tensor(result)}
        text = (f"{self.describe}: {mismatches} of {error.numel()} values outside tolerance; "
                f"max abs error {detail['max_abs_err']:.3e}; worst error/bound ratio {detail['max_err_to_bound_ratio']:.3f}")
        return mismatches == 0, detail, text


def load_submission(entry):
    spec = importlib.util.spec_from_file_location("submission", str(WORKSPACE / "submission.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    function = getattr(module, entry, None)
    if not callable(function):
        raise TypeError(f"Please define a TT-Lang operation named {entry}; see the problem statement for its signature")
    return function


def main():
    spec = json.load(sys.stdin)
    case = Case(spec)
    operation = load_submission(case.entry)
    device = ttnn.open_device(device_id=0)
    try:
        device.enable_program_cache()

        def upload(tensor):
            return ttnn.from_torch(tensor, dtype=ttnn.float32, layout=ttnn.TILE_LAYOUT, device=device,
                                   memory_config=ttnn.DRAM_MEMORY_CONFIG)

        inputs = [upload(case.padded[name]) for name in (("a", "b") if case.exercise == "vector-add" else ("x", "valid"))]
        out = upload(torch.zeros(case.device_shapes["out"]))
        ttnn.synchronize_device(device)

        def invoke():
            result = operation(*inputs, out)
            ttnn.synchronize_device(device)
            return result

        first_start = time.perf_counter_ns()
        invoke()
        first_call_ns = time.perf_counter_ns() - first_start
        first_values = case.crop(ttnn.to_torch(out).to(torch.float32))
        passed, correctness, text = case.check(first_values)
        profile = {"schema_version": 1, "correctness": correctness, "first_call_including_jit_ns": first_call_ns,
                   "exercise": case.exercise, "input_sha256": case.input_sha256,
                   # Bytes the operation must at least move: every padded input read once, the output written once.
                   "traffic": {"required_read_bytes": sum(t.numel() * 4 for t in case.padded.values()),
                               "required_write_bytes": math.prod(case.device_shapes["out"]) * 4}}
        if os.getenv("TT_OJ_PROFILE_ONLY") == "1":
            csv_path = Path(os.getenv("TT_METAL_PROFILER_DIR", "/tmp/generated/profiler")) / ".logs" / "profile_log_device.csv"
            flush = lambda: ttnn.ReadDeviceProfiler(device)  # noqa: E731
            flush()  # export the first invocation separately from warmed samples
            for _ in range(5):
                invoke()
                flush()
            ranges = []
            for sample in range(1, 11):
                before = csv_path.stat().st_size if csv_path.exists() else 0
                invoke()
                flush()  # exporting is outside the device kernel cycle interval
                after = csv_path.stat().st_size if csv_path.exists() else 0
                ranges.append({"sample": sample, "start_byte": before, "end_byte": after})
            final_values = case.crop(ttnn.to_torch(out).to(torch.float32))
            stable = bool(torch.equal(first_values, final_values))
            profile.update({"timing_scope": "device_kernel_cycles", "instrumented": True, "repeatability_verified": stable,
                "device_capture": {"warmup": 5, "sample_count": 10, "selection": "csv_append_byte_ranges",
                    "sample_byte_ranges": ranges,
                    "export_policy": "Profiler flushed after every invocation; export overhead excluded from kernel cycles but changes inter-sample spacing"}})
            if not stable:
                raise RuntimeError("Results changed during the instrumented device pass")
        elif os.getenv("TT_OJ_COUNTERS_ONLY") == "1":
            # Hardware performance counters: the runtime reads them after every compute kernel when
            # TT_METAL_PROFILE_PERF_COUNTERS is set; the judge decodes the rows of the selected samples.
            csv_path = Path(os.getenv("TT_METAL_PROFILER_DIR", "/tmp/generated/profiler")) / ".logs" / "profile_log_device.csv"
            flush = lambda: ttnn.ReadDeviceProfiler(device)  # noqa: E731
            flush()
            for _ in range(2):
                invoke()
                flush()
            ranges = []
            for sample in range(1, 4):
                before = csv_path.stat().st_size if csv_path.exists() else 0
                invoke()
                flush()
                after = csv_path.stat().st_size if csv_path.exists() else 0
                ranges.append({"sample": sample, "start_byte": before, "end_byte": after})
            final_values = case.crop(ttnn.to_torch(out).to(torch.float32))
            stable = bool(torch.equal(first_values, final_values))
            profile.update({"timing_scope": "hardware_counters", "instrumented": True, "repeatability_verified": stable,
                "counter_mask": os.getenv("TT_METAL_PROFILE_PERF_COUNTERS", ""),
                "counter_capture": {"warmup": 2, "sample_count": 3, "selection": "csv_append_byte_ranges",
                                    "sample_byte_ranges": ranges}})
            if not stable:
                raise RuntimeError("Results changed during the counter pass")
        elif os.getenv("TT_OJ_BENCHMARK") == "1":
            warmups = int(os.getenv("TT_OJ_WARMUP", "5"))
            count = int(os.getenv("TT_OJ_SAMPLES", "50"))
            if not 1 <= warmups <= 100 or not 5 <= count <= 1000:
                raise ValueError("Invalid benchmark policy")
            for _ in range(warmups):
                invoke()
            samples = []
            measurement_start_ns = time.perf_counter_ns()
            for _ in range(count):
                start = time.perf_counter_ns()
                invoke()
                samples.append(time.perf_counter_ns() - start)
            measurement_end_ns = time.perf_counter_ns()
            final_values = case.crop(ttnn.to_torch(out).to(torch.float32))
            stable = bool(torch.equal(first_values, final_values))
            profile.update({"timing_scope": "host_submit_to_device_complete",
                "scope_detail": "TT-Lang operation call on preallocated device tensors plus device synchronization; input upload, padding and output readback excluded",
                "clock": "perf_counter", "clock_resolution_ns": round(time.get_clock_info("perf_counter").resolution * 1e9),
                "warmup": warmups, "samples_ns": samples,
                "measurement_start_ns": measurement_start_ns, "measurement_end_ns": measurement_end_ns,
                "repeatability_verified": stable,
                "device_profiler": {"available": os.getenv("TT_OJ_DEVICE_PROFILER_SUPPORTED") == "1"}})
            if not stable:
                raise RuntimeError("Results changed after repeated benchmark runs")
        print(MARKER + json.dumps({"verdict": "pass"} if passed else {"verdict": "fail"}), flush=True)
        print(DETAIL + text.replace("\n", " ")[:1000], flush=True)
        print(PROFILE + json.dumps(profile, allow_nan=False), flush=True)
    finally:
        ttnn.close_device(device)


if __name__ == "__main__":
    main()
