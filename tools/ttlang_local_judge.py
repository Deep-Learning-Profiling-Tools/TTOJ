"""Run the TT-Lang exercise wrapper locally, without Hydro or Docker, the way the worker does.

For each test of a problem it executes a fresh host pass (benchmark samples) and, when the host
pass passes, a fresh instrumented device pass (profiler CSV + NoC traces), then parses both with
the worker's own metrics/device_profile modules. Use it to validate reference solutions and to
produce a prototype report before the Docker runtime image is deployed.

    python3 tools/ttlang_local_judge.py --problem ttlang-vector-add --solution problems/ttlang-vector-add/accepted.py \
        --python /path/to/env/bin/python --cases 1-3 --report out.json
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
from device_profile import parse_device_profile  # noqa: E402
from metrics import summarize_profile  # noqa: E402
from perf_counters import DEFAULT_MASK, parse_perf_counters, summarize_counters  # noqa: E402

MARKER, DETAIL, PROFILE = "TT_OJ_RESULT:", "TT_OJ_DETAIL:", "TT_OJ_PROFILE:"


def device_time_us(device):
    """Same metric as the site: per sample, first kernel start to last kernel end over all cores; median."""
    if not device.get("valid") or not device.get("clock_mhz"):
        return None
    spans = {}
    for span in device.get("core_spans", []):
        key = (span["sample"], span["device_id"], span["program_index"])
        start, end = spans.get(key, (float("inf"), float("-inf")))
        spans[key] = (min(start, span["start_cycle"]), max(end, span["end_cycle"]))
    per_sample = {}
    for (sample, _device, _program), (start, end) in spans.items():
        per_sample[sample] = per_sample.get(sample, 0) + (end - start)
    if not per_sample:
        return None
    values = sorted(per_sample.values())
    middle = len(values) // 2
    median = values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2
    return median / device["clock_mhz"]


def parse_cases(text, count):
    if not text:
        return list(range(1, count + 1))
    chosen = []
    for part in text.split(","):
        lo, _, hi = part.partition("-")
        chosen.extend(range(int(lo), int(hi or lo) + 1))
    return [c for c in chosen if 1 <= c <= count]


def run_pass(python, workspace, case_input, env, timeout):
    started = time.monotonic()
    try:
        completed = subprocess.run([python, "-I", str(workspace / "runner.py")], input=case_input, capture_output=True,
                                   text=True, timeout=timeout, env=env, cwd=str(workspace))
        status = "ok" if completed.returncode == 0 else "runtime_error"
        out, err, code = completed.stdout, completed.stderr, completed.returncode
    except subprocess.TimeoutExpired as expired:
        status, out, err, code = "timeout", expired.stdout or "", expired.stderr or "", None
        if isinstance(out, bytes):
            out, err = out.decode("utf-8", "replace"), (err or b"").decode("utf-8", "replace")
    markers = [line[len(MARKER):] for line in out.splitlines() if line.startswith(MARKER)]
    details = [line[len(DETAIL):] for line in out.splitlines() if line.startswith(DETAIL)]
    profiles = [line[len(PROFILE):] for line in out.splitlines() if line.startswith(PROFILE)]
    return {"status": status, "exit_code": code, "wall_s": round(time.monotonic() - started, 2),
            "result": json.loads(markers[0]) if len(markers) == 1 else None,
            "detail": details[0] if details else None,
            "profile": json.loads(profiles[0]) if len(profiles) == 1 else None,
            "stderr_tail": err[-3000:], "stdout_tail": out[-1500:]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--problem", required=True, help="problem folder name under problems/")
    parser.add_argument("--solution", required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--cases", default="", help="e.g. 1-3,7")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--no-device-pass", action="store_true")
    parser.add_argument("--report", default="")
    args = parser.parse_args()
    manifest = json.loads((ROOT / "problems" / args.problem / "metadata.json").read_text())
    policy = json.loads((ROOT / "worker" / "evaluation-policy.json").read_text())
    tests = manifest["tests"]
    source = Path(args.solution).read_text()
    report = {"problem": manifest["id"], "solution": args.solution, "cases": []}
    try:
        compile(source, "submission.py", "exec")  # the worker rejects syntax errors before any hardware run
    except SyntaxError as error:
        print(f"CE (python syntax): {error}")
        report["verdict"] = "CE"
        if args.report:
            Path(args.report).write_text(json.dumps(report, indent=2))
        return
    base = tempfile.mkdtemp(prefix="ttoj-local-")
    for case_id in parse_cases(args.cases, len(tests)):
        test = tests[case_id - 1]
        entry = {"id": case_id, "input": json.loads(test["input"])}
        for pass_name in (["host"] if args.no_device_pass else ["host", "device", "counters"]):
            if pass_name != "host" and entry.get("host", {}).get("verdict") != "AC":
                break
            workspace = Path(base) / f"{case_id}-{pass_name}"
            workspace.mkdir()
            shutil.copy(ROOT / "sandbox" / "ttlang" / "runner.py", workspace / "runner.py")
            (workspace / "submission.py").write_text(source)
            profiler_dir = workspace / "generated" / "profiler"
            env = {**os.environ, "TT_OJ_WORKSPACE": str(workspace), "PYTHONUNBUFFERED": "1", "TT_METAL_LOGGER_LEVEL": "warning",
                   "TT_OJ_BENCHMARK": "1" if pass_name == "host" else "0", "TT_OJ_PROFILE_ONLY": "1" if pass_name == "device" else "0",
                   "TT_OJ_COUNTERS_ONLY": "1" if pass_name == "counters" else "0",
                   "TT_OJ_WARMUP": str(policy["host"]["warmup_count"]), "TT_OJ_SAMPLES": str(policy["host"]["sample_count"]),
                   "TT_OJ_DEVICE_PROFILER_SUPPORTED": "1", "TT_METAL_PROFILER_DIR": str(profiler_dir), "TT_METAL_HOME": str(workspace)}
            for name in ("TT_METAL_DEVICE_PROFILER", "TT_METAL_DEVICE_PROFILER_NOC_EVENTS", "TT_METAL_PROFILER_MID_RUN_DUMP"):
                env.pop(name, None)
            if pass_name == "counters":
                env["TT_METAL_PROFILE_PERF_COUNTERS"] = str(DEFAULT_MASK)
            if pass_name in ("device", "counters"):
                env.update({"TT_METAL_DEVICE_PROFILER": "1",
                            "TT_METAL_PROFILER_MID_RUN_DUMP": "1", "TT_METAL_PROFILER_DISABLE_PUSH_TO_TRACY": "1",
                            "TT_METAL_PROFILER_CPP_POST_PROCESS": "0"})
            outcome = run_pass(args.python, workspace, test["input"], env, args.timeout)
            expected = json.loads(test["output"])
            if outcome["status"] == "timeout":
                verdict = "TLE"
            elif outcome["status"] != "ok":
                verdict = "CE" if "TTLangCompileError" in outcome["stderr_tail"] or "error:" in outcome["stderr_tail"] else "RE"
            elif outcome["result"] != expected:
                verdict = "WA"
            else:
                verdict = "AC"
            summary = {"verdict": verdict, "wall_s": outcome["wall_s"], "detail": outcome["detail"]}
            if pass_name == "host":
                summary["host"] = summarize_profile(outcome["profile"]) if outcome["profile"] else {"available": False}
            elif pass_name == "counters":
                csv_path = profiler_dir / ".logs" / "profile_log_device.csv"
                capture = (outcome["profile"] or {}).get("counter_capture")
                if csv_path.exists():
                    parsed = parse_perf_counters(csv_path.read_text(), DEFAULT_MASK, capture=capture)
                    summary["counters"] = {"valid": parsed["valid"], "cores": parsed["cores"], "errors": parsed["errors"],
                                           "summary": summarize_counters(parsed)}
                else:
                    summary["counters"] = {"available": False, "reason": "no_csv"}
            else:
                csv_path = profiler_dir / ".logs" / "profile_log_device.csv"
                capture = (outcome["profile"] or {}).get("device_capture")
                if csv_path.exists() and capture:
                    device = parse_device_profile(csv_path.read_text(), capture=capture)
                    summary["device"] = {key: device.get(key) for key in ("available", "valid", "status", "clock_mhz", "errors", "notes")}
                    summary["device"]["core_statistics"] = device.get("core_statistics", [])[:8]
                    summary["device"]["statistics"] = [{k: v for k, v in s.items() if k != "samples_cycles"} for s in device.get("statistics", [])[:12]]
                    summary["device_time_us"] = device_time_us(device)
                    summary["cores"] = len({(s["device_id"], s["core_x"], s["core_y"]) for s in device.get("core_spans", [])})
                else:
                    summary["device"] = {"available": False, "reason": "no_csv_or_capture"}
            if verdict != "AC":
                summary["stderr_tail"] = outcome["stderr_tail"][-1500:]
            entry[pass_name] = summary
            host = summary.get("host", {})
            line = f"case {case_id} [{pass_name}] {verdict} wall={outcome['wall_s']}s"
            if host.get("available"):
                line += f" host_median={host['statistics']['median_ns'] / 1000:.1f}us p95={host['statistics']['p95_ns'] / 1000:.1f}us"
            if summary.get("counters", {}).get("summary"):
                u = summary["counters"]["summary"]["units"]
                line += " " + " ".join(f"{k}={100 * v:.0f}%" for k, v in u.items())
                line += f" any_stall={100 * (summary['counters']['summary'].get('any_thread_stalled') or 0):.0f}%"
            if summary.get("device_time_us"):
                traffic = (outcome["profile"] or {}).get("traffic") or {}
                moved = traffic.get("required_read_bytes", 0) + traffic.get("required_write_bytes", 0)
                line += f" device_time={summary['device_time_us']:.2f}us cores={summary['cores']} bw={moved / (summary['device_time_us'] * 1e3):.1f}GB/s"
            if outcome["detail"]:
                line += f" | {outcome['detail'][:160]}"
            print(line, flush=True)
        report["cases"].append(entry)
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2, default=str))
        print(f"report written to {args.report}")
    shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
