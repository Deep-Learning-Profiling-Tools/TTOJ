"""Trusted TT-Lang exercise wrapper for the CS540 problems.

Only submission.py and this wrapper reach the job. The wrapper owns input
generation, padding, device allocation, the reference computation and the
tolerance check; the submission only provides the TT-Lang operation.

stdin: one JSON batch from the judge worker. Every testcase of a submission runs
in this one process (one device open, one program cache), so container start,
device initialisation and import time are paid once per pass, not per testcase:
  {"pass": "profile" | "counters" | "host", "warmup": 3, "samples": 7,
   "cases": [{"id": 1, "spec": {"exercise": "vector-add", "entry": "vector_add", "seed": 540,
                                "case_id": 1, "N": 1025, "tolerance": {"atol": 1e-6, "rtol": 0.00390625}}},
             {"id": 2, "spec": {"exercise": "ragged-sum", "entry": "ragged_sum", "seed": 540, "case_id": 2,
                                "R": 256, "L": 65, "tolerance": {"atol": 1e-6, "rtol": 0.00390625}}}]}
A bare testcase spec (no "cases" key) is the legacy one-case form selected by the
TT_OJ_PROFILE_ONLY / TT_OJ_COUNTERS_ONLY / TT_OJ_WARMUP / TT_OJ_SAMPLES variables.

Passes: "profile" (device profiler on) checks correctness on the first invocation of each
case, then times warmups and samples both on the host and in the device profiler;
"counters" does the same with the Tensix performance counters compiled into the kernels;
"host" measures host latency without the profiler. A wrong result is never timed, and the
batch stops at the first case that raises, because the device state is not trusted afterwards.

stdout markers (one line each, read by the judge worker):
  TT_OJ_CASE_START:{"id": 3}
  TT_OJ_CASE:{"id": 3, "result": {"verdict": "pass"}, "detail": "...", "profile": {...},
              "csv": "/tmp/tt-oj-cases/case-3-profile.csv", "wall_ns": 1234567}
  TT_OJ_CASE:{"id": 4, "error": "<traceback tail>"}
Legacy single-case runs also print TT_OJ_RESULT: / TT_OJ_DETAIL: / TT_OJ_PROFILE: lines.
"""
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import time
import traceback

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


CASE_START = "TT_OJ_CASE_START:"
CASE_DONE = "TT_OJ_CASE:"
CASES_DIR = Path(os.getenv("TT_OJ_CASE_DIR", "/tmp/tt-oj-cases"))
PASSES = ("host", "profile", "counters")


def emit(marker, payload):
    print(marker + json.dumps(payload, allow_nan=False), flush=True)


class ProfilerCapture:
    """Slices the append-only device profiler CSV into one file per testcase.

    ``ReadDeviceProfiler`` appends the rows of every invocation since the previous read, so a
    testcase's rows are the bytes between the file size before its first invocation and the size
    after its last flush. Each per-case file repeats the two header lines and the sample byte
    ranges are rebased to it, so the judge parses it exactly like a single-case capture.
    """

    def __init__(self, device, pass_name):
        self.device = device
        self.pass_name = pass_name
        self.path = Path(os.getenv("TT_METAL_PROFILER_DIR", "/tmp/generated/profiler")) / ".logs" / "profile_log_device.csv"
        self.header = None

    def size(self):
        return self.path.stat().st_size if self.path.exists() else 0

    def flush(self):
        ttnn.ReadDeviceProfiler(self.device)

    def write_case(self, case_id, start, ranges):
        data = self.path.read_bytes()
        if self.header is None:
            first = data.find(b"\n")
            second = data.find(b"\n", first + 1) if first >= 0 else -1
            if second < 0 or not data[first + 1:second].startswith(b"PCIe slot"):
                raise RuntimeError("Unexpected device profiler CSV layout")
            self.header = data[:second + 1]
        start = max(start, len(self.header))  # the first case's slice begins after the header lines
        CASES_DIR.mkdir(parents=True, exist_ok=True)
        target = CASES_DIR / f"case-{case_id}-{self.pass_name}.csv"
        target.write_bytes(self.header + data[start:])
        shift = len(self.header) - start
        rebased = [{"sample": item["sample"], "start_byte": max(item["start_byte"], start) + shift,
                    "end_byte": item["end_byte"] + shift} for item in ranges]
        return str(target), rebased


def run_case(device, operation, item, pass_name, warmups, samples, capture):
    """Correctness on the first invocation, then warmups and timed samples on the same tensors."""
    case = Case(item["spec"])
    started = time.perf_counter_ns()
    tensors = []

    def upload(tensor):
        value = ttnn.from_torch(tensor, dtype=ttnn.float32, layout=ttnn.TILE_LAYOUT, device=device,
                                memory_config=ttnn.DRAM_MEMORY_CONFIG)
        tensors.append(value)
        return value

    try:
        inputs = [upload(case.padded[name]) for name in (("a", "b") if case.exercise == "vector-add" else ("x", "valid"))]
        out = upload(torch.zeros(case.device_shapes["out"]))
        ttnn.synchronize_device(device)

        def invoke():
            operation(*inputs, out)
            ttnn.synchronize_device(device)

        case_start_byte = capture.size() if capture else 0
        first_start = time.perf_counter_ns()
        invoke()
        first_call_ns = time.perf_counter_ns() - first_start
        if capture:
            capture.flush()  # export the first invocation separately from warmed samples
        first_values = case.crop(ttnn.to_torch(out).to(torch.float32))
        passed, correctness, text = case.check(first_values)
        profile = {"schema_version": 2, "correctness": correctness, "first_call_including_jit_ns": first_call_ns,
                   "exercise": case.exercise, "input_sha256": case.input_sha256,
                   # Bytes the operation must at least move: every padded input read once, the output written once.
                   "traffic": {"required_read_bytes": sum(t.numel() * 4 for t in case.padded.values()),
                               "required_write_bytes": math.prod(case.device_shapes["out"]) * 4}}
        csv_file = None
        if passed:  # a wrong result is never timed
            for _ in range(warmups):
                invoke()
                if capture:
                    capture.flush()
            host_samples, ranges = [], []
            measurement_start_ns = time.perf_counter_ns()
            for sample in range(1, samples + 1):
                before = capture.size() if capture else 0
                start = time.perf_counter_ns()
                invoke()
                host_samples.append(time.perf_counter_ns() - start)
                if capture:
                    capture.flush()  # exporting is outside the timed call and outside the device kernel cycle interval
                    ranges.append({"sample": sample, "start_byte": before, "end_byte": capture.size()})
            measurement_end_ns = time.perf_counter_ns()
            final_values = case.crop(ttnn.to_torch(out).to(torch.float32))
            stable = bool(torch.equal(first_values, final_values))
            profile.update({
                "warmup": warmups, "samples_ns": host_samples, "clock": "perf_counter",
                "clock_resolution_ns": round(time.get_clock_info("perf_counter").resolution * 1e9),
                "measurement_start_ns": measurement_start_ns, "measurement_end_ns": measurement_end_ns,
                "repeatability_verified": stable, "instrumented": capture is not None,
                "timing_scope": "host_submit_to_device_complete",
                "scope_detail": "TT-Lang operation call on preallocated device tensors plus device synchronization, "
                                "measured in the profiler-instrumented pass; input upload, padding, output readback "
                                "and profiler export excluded",
                "device_profiler": {"available": os.getenv("TT_OJ_DEVICE_PROFILER_SUPPORTED") == "1",
                                    "enabled": capture is not None}})
            if capture:
                csv_file, rebased = capture.write_case(item["id"], case_start_byte, ranges)
                selection = {"warmup": warmups, "sample_count": samples, "selection": "csv_append_byte_ranges",
                             "sample_byte_ranges": rebased,
                             "export_policy": "Profiler flushed after every invocation; export overhead excluded "
                                              "from kernel cycles but changes inter-sample spacing"}
                if pass_name == "counters":
                    profile["counter_mask"] = os.getenv("TT_METAL_PROFILE_PERF_COUNTERS", "")
                    profile["counter_capture"] = selection
                else:
                    profile["device_capture"] = selection
            if not stable:
                raise RuntimeError("Results changed between the first and the last invocation")
        return {"id": item["id"], "result": {"verdict": "pass" if passed else "fail"},
                "detail": text.replace("\n", " ")[:1000], "profile": profile, "csv": csv_file,
                "wall_ns": time.perf_counter_ns() - started}
    finally:
        for tensor in tensors:
            try:
                ttnn.deallocate(tensor)
            except Exception:  # noqa: BLE001 - best-effort release between testcases
                pass


def main():
    data = json.load(sys.stdin)
    legacy = "cases" not in data
    if legacy:
        # One case per process, selected by environment, as tools/ttlang_local_judge.py drives it.
        if os.getenv("TT_OJ_PROFILE_ONLY") == "1":
            recipe = ("profile", 5, 10)
        elif os.getenv("TT_OJ_COUNTERS_ONLY") == "1":
            recipe = ("counters", 2, 3)
        else:
            recipe = ("host", int(os.getenv("TT_OJ_WARMUP", "5")), int(os.getenv("TT_OJ_SAMPLES", "50")))
        data = {"pass": recipe[0], "warmup": recipe[1], "samples": recipe[2], "cases": [{"id": 1, "spec": data}]}
    pass_name = data.get("pass", "profile")
    warmups, samples = data.get("warmup", 3), data.get("samples", 7)
    items = data.get("cases")
    if (pass_name not in PASSES or type(warmups) is not int or type(samples) is not int
            or not 0 <= warmups <= 100 or not 1 <= samples <= 1000 or not isinstance(items, list) or not items
            or any(not isinstance(item, dict) or type(item.get("id")) is not int or not isinstance(item.get("spec"), dict)
                   for item in items)):
        raise ValueError("Invalid measurement recipe")
    exercise = items[0]["spec"].get("exercise")
    if exercise not in EXERCISES:
        raise ValueError(f"Unknown exercise {exercise!r}")
    operation = load_submission(items[0]["spec"].get("entry") or EXERCISES[exercise])
    device = ttnn.open_device(device_id=0)
    try:
        device.enable_program_cache()
        capture = ProfilerCapture(device, pass_name) if pass_name != "host" else None
        for item in items:
            emit(CASE_START, {"id": item["id"]})
            try:
                payload = run_case(device, operation, item, pass_name, warmups, samples, capture)
            except Exception:  # noqa: BLE001 - report the failing case, then stop: the device state is not trusted
                if legacy:
                    raise
                emit(CASE_DONE, {"id": item["id"], "error": traceback.format_exc()[-4000:]})
                break
            emit(CASE_DONE, payload)
            if legacy:
                print(MARKER + json.dumps(payload["result"]), flush=True)
                print(DETAIL + payload["detail"], flush=True)
                print(PROFILE + json.dumps(payload["profile"], allow_nan=False), flush=True)
    finally:
        ttnn.close_device(device)


if __name__ == "__main__":
    main()
