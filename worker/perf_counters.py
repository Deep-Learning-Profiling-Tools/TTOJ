"""Decode Tensix hardware performance counters from a device profiler CSV.

With ``TT_METAL_PROFILE_PERF_COUNTERS=<mask>`` the runtime starts the selected counter groups
around every compute kernel and, when the kernel finishes, BRISC reads them and writes one
``TS_DATA`` row per counter (timer id 9090) into ``profile_log_device.csv``. The row carries
``counter_value`` in the low 32 bits of ``data`` and the reference cycle count of the window
in the high 32 bits; the counter identity is not in the CSV, so rows are decoded by their
fixed order: groups in mask-bit order, counters in the architecture table order
(tt_metal/hw/inc/internal/tt-1xx/blackhole/hw_counters.h, tt-metal bundled with tt-lang 1.1.10).
"""
from __future__ import annotations

import csv
import io
import re
import statistics
from collections import defaultdict

PERF_COUNTER_PROFILER_ID = "9090"
_CLOCK = re.compile(r"CHIP_FREQ\s*\[MHz\]\s*:\s*([0-9]+(?:\.[0-9]+)?)", re.I)

# (bit, group name, counter names in table order)
GROUPS = [
    (1 << 0, "fpu", ["FPU_COUNTER", "SFPU_COUNTER", "MATH_COUNTER"]),
    (1 << 1, "pack", ["PACKER0_DEST_READ_REQ", "PACKER_BUSY", "DEST_READ_GRANTED_0",
                      "MATH_NOT_STALLED_DEST_WR_PORT", "MATH_NOT_SCOREBOARD_STALLED"]),
    (1 << 2, "unpack", ["MATH_SRC_DATA_READY", "MATH_NOT_D2S_STALLED", "MATH_INSTRN_STARTED", "MATH_INSTRN_AVAILABLE",
                        "SRCB_WRITE_REQ", "SRCA_WRITE_REQ", "UNPACK0_BUSY_THREAD0", "UNPACK1_BUSY_THREAD0",
                        "UNPACK0_BUSY_THREAD1", "UNPACK1_BUSY_THREAD1", "SRCB_WRITE_NOT_BLOCKED_OVR",
                        "SRCB_WRITE_NOT_BLOCKED_PORT", "SRCA_WRITE_NOT_BLOCKED_OVR", "SRCA_WRITE_NOT_BLOCKED_PORT",
                        "SRCA_WRITE_TID_EVEN", "SRCB_WRITE_TID_EVEN", "SRCA_WRITE_TID_ODD", "SRCB_WRITE_TID_ODD"]),
    (1 << 3, "l1_0", ["L1_0_UNPACKER_0", "L1_0_UNPACKER_1_ECC", "L1_0_TDMA_BUNDLE_0_RISC", "L1_0_TDMA_BUNDLE_1_TRISC",
                      "L1_0_NOC_RING0_OUTGOING_0", "L1_0_NOC_RING0_OUTGOING_1", "L1_0_NOC_RING0_INCOMING_0",
                      "L1_0_NOC_RING0_INCOMING_1", "L1_0_UNPACKER_0_GRANT", "L1_0_UNPACKER_1_ECC_GRANT",
                      "L1_0_TDMA_BUNDLE_0_GRANT", "L1_0_TDMA_BUNDLE_1_GRANT", "L1_0_NOC_RING0_OUTGOING_0_GRANT",
                      "L1_0_NOC_RING0_OUTGOING_1_GRANT", "L1_0_NOC_RING0_INCOMING_0_GRANT", "L1_0_NOC_RING0_INCOMING_1_GRANT"]),
    (1 << 4, "l1_1", ["L1_1_PACKER_IF_0", "L1_1_UNPACKER1_EXT_IF_1", "L1_1_UNPACKER1_EXT_IF_2", "L1_1_UNPACKER1_EXT_IF_3",
                      "L1_1_NOC_RING1_OUTGOING_0", "L1_1_NOC_RING1_OUTGOING_1", "L1_1_NOC_RING1_INCOMING_0",
                      "L1_1_NOC_RING1_INCOMING_1", "L1_1_PACKER_IF_0_GRANT", "L1_1_UNPACKER1_EXT_IF_1_GRANT",
                      "L1_1_UNPACKER1_EXT_IF_2_GRANT", "L1_1_UNPACKER1_EXT_IF_3_GRANT", "L1_1_NOC_RING1_OUTGOING_0_GRANT",
                      "L1_1_NOC_RING1_OUTGOING_1_GRANT", "L1_1_NOC_RING1_INCOMING_0_GRANT", "L1_1_NOC_RING1_INCOMING_1_GRANT"]),
    (1 << 5, "instrn", ["CFG_INSTRN_AVAILABLE_0", "CFG_INSTRN_AVAILABLE_1", "CFG_INSTRN_AVAILABLE_2",
                        "SYNC_INSTRN_AVAILABLE_0", "SYNC_INSTRN_AVAILABLE_1", "SYNC_INSTRN_AVAILABLE_2",
                        "THCON_INSTRN_AVAILABLE_0", "THCON_INSTRN_AVAILABLE_1", "THCON_INSTRN_AVAILABLE_2",
                        "MOVE_INSTRN_AVAILABLE_0", "MOVE_INSTRN_AVAILABLE_1", "MOVE_INSTRN_AVAILABLE_2",
                        "MATH_INSTRN_AVAILABLE_0", "MATH_INSTRN_AVAILABLE_1", "MATH_INSTRN_AVAILABLE_2",
                        "UNPACK_INSTRN_AVAILABLE_0", "UNPACK_INSTRN_AVAILABLE_1", "UNPACK_INSTRN_AVAILABLE_2",
                        "PACK_INSTRN_AVAILABLE_0", "PACK_INSTRN_AVAILABLE_1", "PACK_INSTRN_AVAILABLE_2",
                        "THREAD_STALLS_0", "THREAD_STALLS_1", "THREAD_STALLS_2",
                        "WAITING_FOR_SRCA_CLEAR", "WAITING_FOR_SRCB_CLEAR", "WAITING_FOR_SRCA_VALID", "WAITING_FOR_SRCB_VALID",
                        "WAITING_FOR_THCON_IDLE_0", "WAITING_FOR_UNPACK_IDLE_0", "WAITING_FOR_PACK_IDLE_0", "WAITING_FOR_MATH_IDLE_0",
                        "WAITING_FOR_NONZERO_SEM_0", "WAITING_FOR_NONFULL_SEM_0", "WAITING_FOR_MOVE_IDLE_0", "WAITING_FOR_CFG_IDLE_0",
                        "WAITING_FOR_SFPU_IDLE_0", "WAITING_FOR_THCON_IDLE_1", "WAITING_FOR_UNPACK_IDLE_1", "WAITING_FOR_PACK_IDLE_1",
                        "WAITING_FOR_MATH_IDLE_1", "WAITING_FOR_NONZERO_SEM_1", "WAITING_FOR_NONFULL_SEM_1", "WAITING_FOR_MOVE_IDLE_1",
                        "WAITING_FOR_CFG_IDLE_1", "WAITING_FOR_SFPU_IDLE_1", "WAITING_FOR_THCON_IDLE_2", "WAITING_FOR_UNPACK_IDLE_2",
                        "WAITING_FOR_PACK_IDLE_2", "WAITING_FOR_MATH_IDLE_2", "WAITING_FOR_NONZERO_SEM_2", "WAITING_FOR_NONFULL_SEM_2",
                        "WAITING_FOR_MOVE_IDLE_2", "WAITING_FOR_CFG_IDLE_2", "WAITING_FOR_SFPU_IDLE_2",
                        "THREAD_INSTRUCTIONS_0", "THREAD_INSTRUCTIONS_1", "THREAD_INSTRUCTIONS_2", "ANY_THREAD_STALL"]),
]
DEFAULT_MASK = (1 << 0) | (1 << 1) | (1 << 2) | (1 << 5)  # fpu, pack, unpack, instrn: readable in one pass

# Counters that are event counts rather than busy cycles (shown as rates, not percentages).
COUNT_COUNTERS = {"THREAD_INSTRUCTIONS_0", "THREAD_INSTRUCTIONS_1", "THREAD_INSTRUCTIONS_2", "PACKER0_DEST_READ_REQ",
                  "DEST_READ_GRANTED_0", "SRCA_WRITE_REQ", "SRCB_WRITE_REQ", "MATH_INSTRN_STARTED",
                  "SRCA_WRITE_TID_EVEN", "SRCB_WRITE_TID_EVEN", "SRCA_WRITE_TID_ODD", "SRCB_WRITE_TID_ODD"}

THREADS = {"0": "unpack", "1": "math", "2": "pack"}


def counter_names(mask: int) -> list[str]:
    names = []
    for bit, _group, table in GROUPS:
        if mask & bit:
            names.extend(table)
    return names


def _byte_ranges(raw: bytes, capture):
    ranges = []
    if not isinstance(capture, dict) or capture.get("selection") != "csv_append_byte_ranges":
        return None
    for item in capture.get("sample_byte_ranges") or []:
        sample, start, end = item.get("sample"), item.get("start_byte"), item.get("end_byte")
        if any(type(v) is not int for v in (sample, start, end)) or not 0 <= start < end <= len(raw):
            return None
        ranges.append((sample, start, end))
    return ranges or None


def parse_perf_counters(data: str | bytes, mask: int = DEFAULT_MASK, capture: dict | None = None) -> dict:
    """Return per-counter utilisation across cores and samples.

    Rows outside the capture byte ranges (first invocation, warmups) are ignored when a capture
    is given. The result keeps every core's mean so that hot and idle cores stay visible.
    """
    raw = data if isinstance(data, bytes) else data.encode("utf-8")
    text = raw.decode("utf-8", errors="replace")
    clocks = [float(v) for v in _CLOCK.findall(text)]
    clock_mhz = clocks[0] if clocks and all(c == clocks[0] for c in clocks) else None
    names = counter_names(mask)
    ranges = _byte_ranges(raw, capture)
    offsets = [0]
    for line in raw.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    # (device, core, run) -> list of (value, ref) in arrival order
    runs: dict[tuple, list[tuple[int, int]]] = defaultdict(list)
    run_sample: dict[tuple, int | None] = {}
    errors = []
    header = None
    previous_line = 0
    reader = csv.reader(io.StringIO(text), skipinitialspace=True)
    for row in reader:
        row_start = offsets[previous_line]
        row_end = offsets[min(reader.line_num, len(offsets) - 1)]
        previous_line = reader.line_num
        if not row or not any(row) or row[0].lower().startswith("arch:"):
            continue
        if row[0].lower() in {"pcie slot", "device id", "device_id"}:
            header = [c.lower() for c in row]
            continue
        if header is None or len(row) != len(header):
            continue
        values = dict(zip(header, (c.strip() for c in row)))
        if values.get("timer_id") != PERF_COUNTER_PROFILER_ID or values.get("type", "").upper() != "TS_DATA":
            continue
        try:
            device = int(values.get("pcie slot") or values.get("device id") or values.get("device_id"))
            core = (int(values["core_x"]), int(values["core_y"]))
            run = values.get("run host id") or values.get("run id") or ""
            packed = int(values["data"])
        except (KeyError, ValueError):
            errors.append("malformed_counter_row")
            continue
        sample = None
        if ranges is not None:
            sample = next((s for s, start, end in ranges if start <= row_start and row_end <= end), None)
            if sample is None:
                continue
        key = (device, core, run)
        runs[key].append((packed & 0xFFFFFFFF, packed >> 32))
        run_sample[key] = sample
    per_core: dict[tuple, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    windows = []
    decoded_runs = 0
    for key, readings in runs.items():
        if len(readings) % len(names) != 0:
            errors.append(f"unexpected_counter_row_count:{len(readings)}")
            continue
        for offset in range(0, len(readings), len(names)):
            block = readings[offset:offset + len(names)]
            refs = [ref for _, ref in block if ref > 0]
            if not refs:
                errors.append("zero_reference_window")
                continue
            window = statistics.median(refs)
            windows.append(window)
            decoded_runs += 1
            for name, (value, ref) in zip(names, block):
                if name in COUNT_COUNTERS:
                    per_core[(key[0], key[1])][name].append(value / (ref or window))  # events per cycle
                else:
                    per_core[(key[0], key[1])][name].append(min(value / (ref or window), 1.0))
    counters = {}
    for name in names:
        core_means = [statistics.mean(v[name]) for v in per_core.values() if v.get(name)]
        if not core_means:
            continue
        entry = {"mean": statistics.mean(core_means), "max_core": max(core_means), "min_core": min(core_means),
                 "unit": "events_per_cycle" if name in COUNT_COUNTERS else "fraction_of_window"}
        counters[name] = entry
    return {
        "available": bool(counters), "valid": bool(counters) and not errors, "mask": mask,
        "groups": [g for bit, g, _ in GROUPS if mask & bit], "clock_mhz": clock_mhz,
        "cores": len(per_core), "windows_decoded": decoded_runs,
        "window_cycles_median": statistics.median(windows) if windows else None,
        "window_us_median": (statistics.median(windows) / clock_mhz) if windows and clock_mhz else None,
        "counters": counters, "errors": sorted(set(errors)),
        "notes": ["Fractions are counter cycles divided by the counter window (compute kernel start to end on that core), "
                  "averaged over the measured samples; 'max_core' is the busiest core's average.",
                  "Thread 0 = unpack (TRISC0), thread 1 = math (TRISC1), thread 2 = pack (TRISC2).",
                  "Counters are read in a separate pass with the device profiler enabled; they are not the ranked device time."],
    }


def summarize_counters(parsed: dict) -> dict | None:
    """Compact, report-friendly view: unit utilisation, per-thread stalls, and the top stall reasons."""
    if not parsed or not parsed.get("available"):
        return None
    c = parsed["counters"]
    get = lambda name, field="mean": c.get(name, {}).get(field)  # noqa: E731
    units = {
        "fpu_busy": get("FPU_COUNTER"), "sfpu_busy": get("SFPU_COUNTER"), "math_busy": get("MATH_COUNTER"),
        "unpacker0_busy": max(x for x in (get("UNPACK0_BUSY_THREAD0"), get("UNPACK0_BUSY_THREAD1")) if x is not None) if c.get("UNPACK0_BUSY_THREAD0") else None,
        "unpacker1_busy": max(x for x in (get("UNPACK1_BUSY_THREAD0"), get("UNPACK1_BUSY_THREAD1")) if x is not None) if c.get("UNPACK1_BUSY_THREAD0") else None,
        "packer_busy": get("PACKER_BUSY"),
    }
    threads = {}
    for tid, label in THREADS.items():
        reasons = {}
        for reason in ("THCON", "UNPACK", "PACK", "MATH", "MOVE", "CFG", "SFPU"):
            value = get(f"WAITING_FOR_{reason}_IDLE_{tid}")
            if value is not None:
                reasons[f"waiting_for_{reason.lower()}_idle"] = value
        for sem in ("NONZERO", "NONFULL"):
            value = get(f"WAITING_FOR_{sem}_SEM_{tid}")
            if value is not None:
                reasons[f"waiting_for_{sem.lower()}_semaphore"] = value
        threads[label] = {"stalled": get(f"THREAD_STALLS_{tid}"),
                          "instructions_per_cycle": get(f"THREAD_INSTRUCTIONS_{tid}"),
                          "reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1]))}
    for name in ("WAITING_FOR_SRCA_VALID", "WAITING_FOR_SRCB_VALID", "WAITING_FOR_SRCA_CLEAR", "WAITING_FOR_SRCB_CLEAR"):
        if name in c:
            threads["math"]["reasons"][name.lower()] = c[name]["mean"]
    threads["math"]["reasons"] = dict(sorted(threads["math"]["reasons"].items(), key=lambda kv: -kv[1]))
    return {"units": {k: v for k, v in units.items() if v is not None}, "threads": threads,
            "any_thread_stalled": get("ANY_THREAD_STALL"), "cores": parsed.get("cores"),
            "window_us": parsed.get("window_us_median"), "valid": parsed.get("valid")}
