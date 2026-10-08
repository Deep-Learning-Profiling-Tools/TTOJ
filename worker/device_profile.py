"""Parse official Metalium device-profiler CSV without summing parallel RISCs."""

from __future__ import annotations

import csv
import io
import math
import re
import statistics
from collections import defaultdict


_CLOCK = re.compile(r"CHIP_FREQ\s*\[MHz\]\s*:\s*([0-9]+(?:\.[0-9]+)?)", re.I)
_BEGIN = {"begin", "zone_start", "start"}
_END = {"end", "zone_end"}


def _duration_statistics(values: list[int], clock_mhz: float | None) -> dict:
    ordered = sorted(values)
    position = (len(ordered) - 1) * 0.95
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    p95 = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    result = {"count": len(values), "samples_cycles": values,
              "min_cycles": min(values), "median_cycles": statistics.median(values),
              "p95_cycles": p95, "max_cycles": max(values), "mean_cycles": statistics.mean(values),
              "stdev_cycles": statistics.stdev(values) if len(values) > 1 else 0}
    for statistic in ("min", "median", "p95", "max", "mean", "stdev"):
        result[f"{statistic}_ns"] = result[f"{statistic}_cycles"] * 1000.0 / clock_mhz if clock_mhz else None
    return result


def parse_device_profile(data: str | bytes, capture: dict | None = None) -> dict:
    """Return per-kernel cycles and frequency-based ns from a raw CSV payload.

    v0.79 fields are accepted, along with older ``Run ID``/``zone phase``
    aliases. Invalid or unmatched markers remain visible in ``errors``;
    ``available`` means valid kernel pairs exist, while ``valid`` means the
    complete CSV and selection had no malformed, misordered or unpaired records.
    """
    raw_data = data if isinstance(data, bytes) else data.encode("utf-8")
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")
    clocks = [float(value) for value in _CLOCK.findall(data)]
    clock_mhz = clocks[0] if clocks and all(value == clocks[0] for value in clocks) else None
    if clock_mhz is not None and (not math.isfinite(clock_mhz) or clock_mhz <= 0):
        clock_mhz = None
    notes = [
        "Device cycle counters are paired within the same device, core, RISC and run.",
        "duration_ns is derived from the CSV's recorded MHz; changing clock frequency adds uncertainty.",
        "Per-RISC durations include kernel waits and data movement. Parallel RISC durations are not summed.",
        "No cross-device clock synchronization or total elapsed time is inferred.",
    ]
    if not clocks:
        notes.append("No recorded CHIP_FREQ[MHz]; duration_ns is unavailable.")
    elif clock_mhz is None:
        notes.append("Conflicting or invalid recorded clock frequencies; duration_ns is unavailable.")

    errors = []
    ranges = []
    capture_valid = True
    capture_requested = capture is not None
    if capture is None:
        capture = {"selection": "none", "status": "unselected_cold_diagnostic", "warmup": None, "sample_count": None}
        notes.append("Unselected cold diagnostic: capture metadata is absent; cold initialization and warmups may be included.")
    else:
        capture = dict(capture) if isinstance(capture, dict) else {}
        try:
            if capture.get("selection") != "csv_append_byte_ranges":
                raise ValueError("unsupported capture selection")
            count = capture.get("sample_count")
            declared = capture.get("sample_byte_ranges")
            if type(count) is not int or count < 1 or not isinstance(declared, list) or len(declared) != count:
                raise ValueError("sample_count does not match byte ranges")
            previous_end = -1
            seen_samples = set()
            for item in declared:
                sample, start, end = item["sample"], item["start_byte"], item["end_byte"]
                if any(type(value) is not int for value in (sample, start, end)):
                    raise ValueError("sample and byte offsets must be integers")
                if not 1 <= sample <= count or sample in seen_samples or not 0 <= start < end <= len(raw_data):
                    raise ValueError("invalid sample identity or byte range")
                if start < previous_end:
                    raise ValueError("overlapping or misordered byte ranges")
                if (start and raw_data[start - 1:start] != b"\n") or raw_data[end - 1:end] != b"\n":
                    raise ValueError("byte range splits a CSV row")
                previous_end = end
                seen_samples.add(sample)
                ranges.append((sample, start, end))
        except (ValueError, TypeError, KeyError) as error:
            capture_valid = False
            errors.append({"reason": "invalid_capture_metadata", "details": str(error)})
    # Any supplied metadata is a selection request. A malformed request must
    # never quietly become an unfiltered diagnostic capture.
    selected = capture_requested
    physical_offsets = [0]
    for line in raw_data.splitlines(keepends=True):
        physical_offsets.append(physical_offsets[-1] + len(line))
    previous_physical_line = 0
    kernels = []
    header = None
    stacks = defaultdict(list)
    last_timestamp = {}
    seen = set()
    stream = csv.reader(io.StringIO(data), skipinitialspace=True)
    for row_number, raw_row in enumerate(stream, start=1):
        row_start_byte = physical_offsets[previous_physical_line]
        row_end_byte = physical_offsets[min(stream.line_num, len(physical_offsets) - 1)]
        previous_physical_line = stream.line_num
        sample = next((index for index, start, end in ranges if start <= row_start_byte and row_end_byte <= end), None)
        row = [value.strip() for value in raw_row]
        if not row or not any(row):
            continue
        if row[0].lower().startswith("arch:"):
            continue
        if row[0].lower() in {"pcie slot", "device id", "device_id"}:
            header = [value.lower() for value in row]
            continue
        if header is None:
            errors.append({"row": row_number, "reason": "data_before_csv_header"})
            continue
        if len(row) != len(header):
            errors.append({"row": row_number, "reason": "malformed_column_count", "expected": len(header), "actual": len(row)})
            continue
        values = dict(zip(header, row))

        def value(*aliases, default=""):
            return next((values[name] for name in aliases if name in values), default)

        zone = value("zone name", "zone")
        # Firmware owns the enclosing *-KERNEL zones. User signposts, firmware
        # loops, dispatch zones and profiler plumbing are not kernel durations.
        if not zone.endswith("-KERNEL"):
            continue
        phase = value("type", "zone phase", "phase").lower()
        if phase not in _BEGIN | _END:
            continue
        try:
            device = int(value("pcie slot", "device id", "device_id"))
            core_x, core_y = int(value("core_x")), int(value("core_y"))
            timestamp = int(value("time[cycles since reset]", "timestamp", "time"))
            if timestamp < 0:
                raise ValueError("negative timestamp")
        except ValueError:
            errors.append({"row": row_number, "reason": "invalid_identity_or_cycle_timestamp", "zone": zone})
            continue
        risc = value("risc processor type", "risc")
        run = value("run host id", "run id", "run_id")
        trace_id = value("trace id")
        trace_counter = value("trace id counter")
        source = value("source file")
        source_line = value("source line")
        identity = (device, core_x, core_y, risc, run, trace_id, trace_counter)
        pair_key = identity + (zone, source, source_line)
        record_key = pair_key + (phase, timestamp)
        if record_key in seen:
            errors.append({"row": row_number, "reason": "duplicate_marker", "zone": zone, "risc": risc, "run_id": run})
            continue
        seen.add(record_key)
        previous = last_timestamp.get(identity)
        if previous is not None and timestamp < previous:
            errors.append({"row": row_number, "reason": "misordered_timestamp", "zone": zone,
                           "risc": risc, "run_id": run, "previous_cycle": previous, "cycle": timestamp})
        last_timestamp[identity] = timestamp
        if phase in _BEGIN:
            stacks[pair_key].append({"cycle": timestamp, "row": row_number,
                                     "start_byte": row_start_byte, "sample": sample})
            continue
        if not stacks[pair_key]:
            errors.append({"row": row_number, "reason": "unpaired_end_marker", "zone": zone, "risc": risc, "run_id": run})
            continue
        start = stacks[pair_key].pop()
        if timestamp < start["cycle"]:
            errors.append({"row": row_number, "reason": "end_precedes_begin", "zone": zone, "risc": risc, "run_id": run})
            continue
        cycles = timestamp - start["cycle"]
        if selected and sample != start["sample"]:
            errors.append({"row": row_number, "reason": "pair_crosses_capture_boundary", "zone": zone, "risc": risc, "run_id": run})
            continue
        kernels.append({
            "device_id": device, "core_x": core_x, "core_y": core_y,
            "risc": risc, "zone": zone, "run_id": int(run) if run.isdigit() else run,
            "trace_id": trace_id or None, "trace_id_counter": trace_counter or None,
            "source_file": source, "source_line": int(source_line) if source_line.isdigit() else None,
            "start_cycle": start["cycle"], "end_cycle": timestamp,
            "duration_cycles": cycles,
            "duration_ns": cycles * 1000.0 / clock_mhz if clock_mhz else None,
            "sample": sample, "csv_start_byte": start["start_byte"], "csv_end_byte": row_end_byte,
        })
    for key, entries in stacks.items():
        for start in entries:
            errors.append({"row": start["row"], "reason": "unpaired_begin_marker", "zone": key[7],
                           "risc": key[3], "run_id": key[4], "start_cycle": start["cycle"]})

    raw_kernel_count = len(kernels)
    if selected:
        kernels = [item for item in kernels if item["sample"] is not None] if capture_valid else []
        samples_present = {item["sample"] for item in kernels}
        missing = sorted({item[0] for item in ranges} - samples_present)
        if missing:
            errors.append({"reason": "missing_selected_samples", "samples": missing})
    occurrences = defaultdict(list)
    for kernel in kernels:
        key = (kernel["device_id"], kernel["core_x"], kernel["core_y"],
               kernel["risc"], kernel["zone"], kernel["sample"])
        occurrences[key].append(kernel)
    for items in occurrences.values():
        for index, kernel in enumerate(sorted(items, key=lambda item: item["start_cycle"]), start=1):
            kernel["program_index"] = index if selected else None
    if selected:
        notes.append("program_index is the 1-based kernel occurrence within a sample on this core/RISC/zone, not a global program ID.")
        notes.append("Statistics describe one ordered kernel stage across measured samples; they are not total time per solve invocation.")
    groups = defaultdict(list)
    for kernel in kernels:
        key = (kernel["device_id"], kernel["core_x"], kernel["core_y"], kernel["run_id"],
               kernel["trace_id"], kernel["trace_id_counter"], kernel["sample"], kernel["program_index"])
        groups[key].append(kernel)
    core_spans = []
    for key, items in groups.items():
        if key[6] is None and len({item["risc"] for item in items}) != len(items):
            notes.append(f"Run {key[3]} reuses a RISC on core {key[1]},{key[2]}; no combined envelope without sample selection.")
            continue
        start = min(item["start_cycle"] for item in items)
        end = max(item["end_cycle"] for item in items)
        core_spans.append({
            "device_id": key[0], "core_x": key[1], "core_y": key[2], "run_id": key[3],
            "trace_id": key[4], "trace_id_counter": key[5],
            "sample": key[6],
            "program_index": key[7],
            "timing_scope": "same_core_kernel_envelope", "risc_count": len({item["risc"] for item in items}),
            "start_cycle": start, "end_cycle": end, "duration_cycles": end - start,
            "duration_ns": (end - start) * 1000.0 / clock_mhz if clock_mhz else None,
        })
    summary_groups = defaultdict(list)
    for kernel in kernels:
        summary_groups[(kernel["device_id"], kernel["core_x"], kernel["core_y"], kernel["risc"], kernel["zone"], kernel["program_index"])].append(kernel["duration_cycles"])
    summaries = [{"device_id": key[0], "core_x": key[1], "core_y": key[2], "risc": key[3], "zone": key[4],
                  "program_index": key[5], "expected_sample_count": capture.get("sample_count") if selected else None,
                  "complete": len(values) == capture.get("sample_count") if selected else None,
                  **_duration_statistics(values, clock_mhz)} for key, values in summary_groups.items()]
    span_groups = defaultdict(list)
    for span in core_spans:
        span_groups[(span["device_id"], span["core_x"], span["core_y"], span["program_index"])].append(span["duration_cycles"])
    span_statistics = [{"device_id": key[0], "core_x": key[1], "core_y": key[2],
                        "timing_scope": "same_core_kernel_envelope",
                        "program_index": key[3],
                        **_duration_statistics(values, clock_mhz)} for key, values in span_groups.items()]
    if selected and any(not summary["complete"] for summary in summaries):
        notes.append("Some kernel stages are missing from measured samples; incomplete groups cannot establish consistent stage alignment.")
    valid = bool(kernels) and not errors and capture_valid
    if errors:
        notes.append("Malformed, misordered, duplicate or unmatched markers make this capture incomplete; consult errors.")
    return {
        "available": bool(kernels), "valid": valid,
        "status": "ok" if valid else "partial" if kernels else "invalid" if errors else "unavailable",
        "clock_mhz": clock_mhz, "timing_scope": "device_kernel_cycles",
        "kernels": kernels, "core_spans": core_spans, "errors": errors, "notes": list(dict.fromkeys(notes)),
        "capture": capture, "raw_kernel_count": raw_kernel_count,
        "statistics": summaries, "core_statistics": span_statistics,
    }
