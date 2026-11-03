"""Benchmark statistics, provenance and host telemetry (never hidden answers)."""
import hashlib
import math
import os
from pathlib import Path
import random
import re
import statistics
import time


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def numeric_block(value):
    """Keep only finite numeric fields with simple keys from a wrapper-provided object."""
    if not isinstance(value, dict):
        return None
    return {key: item for key, item in value.items()
            if re.fullmatch(r"[a-z0-9_]{1,64}", str(key)) and type(item) in (int, float, bool) and math.isfinite(item)}


def percentile(values, p):
    ordered = sorted(values)
    position = (len(ordered) - 1) * p
    lo, hi = math.floor(position), math.ceil(position)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo)


def summarize_profile(profile):
    if not isinstance(profile, dict):
        return {"available": False, "reason": "profile_is_not_an_object"}
    values = profile.get("samples_ns", [])
    if not values or len(values) > 10000 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in values):
        return {"available": False, "reason": "invalid_or_missing_samples"}
    median = statistics.median(values)
    mean = statistics.mean(values)
    stdev = statistics.stdev(values) if len(values) > 1 else 0
    # Deterministic bootstrap; an empirical interval, not a hardware accuracy
    # guarantee. Consecutive samples can be correlated; state this in the UI.
    rng = random.Random(0)
    medians = [statistics.median(rng.choices(values, k=len(values))) for _ in range(1000)]
    # Submitted Python shares the wrapper process and can emit arbitrary JSON.
    # Public record visibility is weaker than source visibility, so never copy
    # free text or unknown fields from an output marker into a public report.
    metadata = {"samples_ns": values, "units": "ns"}
    for name in ("schema_version", "warmup", "warmup_count", "clock_resolution_ns",
                 "first_call_including_jit_ns", "measurement_start_ns", "measurement_end_ns",
                 "device_clock_mhz", "device_clock_mhz_end"):
        value = profile.get(name)
        if value is None:
            if name in profile:
                metadata[name] = None
        elif type(value) in (int, float) and math.isfinite(value) and value >= 0:
            metadata[name] = value
    for name in ("first_correct", "final_correct", "correctness_verified", "instrumented", "repeatability_verified"):
        if type(profile.get(name)) is bool:
            metadata[name] = profile[name]
    scope = profile.get("timing_scope")
    metadata["timing_scope"] = scope if scope in ("host_submit_to_device_complete", "device_kernel_cycles") else "unknown_redacted"
    # TT-Lang exercise wrappers add numeric-only correctness and perf-summary blocks; copy numbers, never free text.
    for block in ("correctness", "traffic"):
        if isinstance(profile.get(block), dict):
            metadata[block] = numeric_block(profile[block])
    if profile.get("exercise") in ("vector-add", "ragged-sum", "ragged-softmax"):
        metadata["exercise"] = profile["exercise"]
    clock = profile.get("clock")
    if clock in ("perf_counter", "steady_clock"):
        metadata["clock"] = clock
    profiler = profile.get("device_profiler")
    if isinstance(profiler, dict):
        metadata["device_profiler"] = {name: profiler[name] for name in ("available", "enabled") if type(profiler.get(name)) is bool}
        if profiler.get("reason") in ("disabled_for_host_benchmark", "packaged_library_profiler_stubs"):
            metadata["device_profiler"]["reason"] = profiler["reason"]
    return {
        **metadata, "available": True,
        "warmup_count": metadata.get("warmup", metadata.get("warmup_count", 0)),
        "sample_count": len(values),
        "statistics": {
            "min_ns": min(values), "median_ns": median, "mean_ns": mean,
            "p95_ns": percentile(values, 0.95), "max_ns": max(values),
            "mad_ns": statistics.median(abs(v - median) for v in values),
            "stdev_ns": stdev, "cv_percent": 100 * stdev / mean,
            "median_ci95_ns": [percentile(medians, 0.025), percentile(medians, 0.975)],
        },
        "samples_are_independent": False,
        "outlier_policy": "keep_all_samples",
        "confidence_interval_method": "deterministic_bootstrap_1000_assumes_iid",
        "accuracy_guarantee_ns": None,
    }


def telemetry_snapshot(sys_root="/host-sys"):
    root = Path(sys_root)
    result = {"monotonic_ns": time.monotonic_ns(), "host_loadavg": list(os.getloadavg()), "devices": [], "cpu_governors": {}}
    def read(path):
        try:
            return path.read_text().strip()
        except (OSError, ValueError):
            return None
    for entry in sorted((root / "class/tenstorrent").glob("tenstorrent!*")):
        result["devices"].append({"device_id": int(entry.name.split("!")[-1]),
            "aiclk_mhz": read(entry / "tt_aiclk"), "firmware_version": read(entry / "tt_fw_bundle_ver"),
            "thermal_trip_count": read(entry / "tt_therm_trip_count"), "card_type": read(entry / "tt_card_type")})
    result["temperatures_c"] = []
    for hwmon in sorted((root / "class/hwmon").glob("hwmon*")):
        if read(hwmon / "name") != "blackhole":
            continue
        for sensor in sorted(hwmon.glob("temp*_input")):
            try:
                result["temperatures_c"].append({"sensor": f"{hwmon.name}/{sensor.name}", "value": float(read(sensor)) / 1000})
            except (ValueError, TypeError):
                pass
    for cpu in (root / "devices/system/cpu").glob("cpu[0-9]*"):
        value = read(cpu / "cpufreq/scaling_governor")
        if value is not None:
            result["cpu_governors"][cpu.name] = value
    result["driver_version"] = read(root / "module/tenstorrent/version")
    return result


def quality_report(cases):
    warnings = [
        "Host CPU sets separate OJ services but do not isolate OS tasks or IRQs.",
        "The mesh lock coordinates OJ workers; host programs outside OJ are not fully excluded.",
        "Device cycles and host latencies are measured with the device profiler enabled; host latencies include its overhead and are diagnostic only.",
        "Nanoseconds are storage units and clock resolution, not a nanosecond accuracy guarantee.",
    ]
    if any(c.get("host", {}).get("statistics", {}).get("cv_percent", 0) > 5 for c in cases):
        warnings.append("Host sample coefficient of variation exceeds 5%; retain raw samples and rerun before comparing close results.")
    if any(not c.get("device", {}).get("valid") for c in cases):
        warnings.append("Some cases do not have validated device profiler events.")
    if any(c.get("runtime_affinity_warning") for c in cases):
        warnings.append("The SDK reported a thread affinity failure; container CPU affinity still applies, but per-thread placement is not assured.")
    reasons = ["cpu_irq_isolation_not_verified", "external_host_workloads_not_fully_excluded",
               "clock_frequency_not_locked_or_calibrated", "device_profiler_is_instrumented",
               "cross_core_total_device_elapsed_not_calibrated", "submission_wrapper_not_tamper_attested"]
    if not cases:
        reasons.append("no_completed_cases")
    if any(c.get("status") != 1 for c in cases):
        reasons.append("correctness_not_all_passed")
    if any(not c.get("host", {}).get("available") for c in cases):
        reasons.append("host_samples_unavailable")
    if any(c.get("host", {}).get("statistics", {}).get("cv_percent", 0) > 5 for c in cases):
        reasons.append("host_cv_exceeds_5_percent")
    if any(not c.get("device", {}).get("valid") for c in cases):
        reasons.append("device_capture_not_validated")
    if any(c.get("runtime_affinity_warning") for c in cases):
        reasons.append("sdk_thread_affinity_warning")
    if any(c.get("policy_validation", {}).get("matches") is False for c in cases):
        reasons.append("measurement_recipe_mismatch")
    if any(not c.get("correctness", {}).get("reference_available", True) for c in cases):
        reasons.append("self_test_has_no_reference")
    return {"eligible_for_ranking": False, "purpose": "diagnostic_correctness_and_profiling",
            "reason_codes": reasons,
            "warnings": warnings, "formal_ranking_policy": "not_enabled_pending_external_isolation_and_calibration"}
