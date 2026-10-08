"""Run fresh OJ submissions repeatedly; preserve host and device metrics separately.

This is an administrator's local experiment, not a performance leaderboard.
The ordinary OJ queue, correctness checks and hardware reservation remain active.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import statistics
import uuid

from check_e2e import Client, ROOT, STATUS


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(value, output, ensure_ascii=False, indent=2, allow_nan=False)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def aggregate(entries):
    groups = {}
    for entry in entries:
        report = entry.get("report", {})
        fingerprint = json.dumps({
            "protocol": report.get("protocol", {}).get("sha256"),
            "manifest": report.get("environment", {}).get("manifest_sha256"),
            "images": report.get("environment", {}).get("image_ids"),
            "code_hashes": {name: value for name, value in report.get("environment", {}).items()
                            if name.endswith("_sha256") and name != "source_sha256"},
            "driver_version": report.get("environment", {}).get("before", {}).get("driver_version"),
            "device_versions": [{name: device.get(name) for name in ("device_id", "firmware_version", "card_type")}
                                for device in report.get("environment", {}).get("before", {}).get("devices", [])],
            "cpu_governors": report.get("environment", {}).get("before", {}).get("cpu_governors"),
            "host_kernel": report.get("environment", {}).get("host_kernel"),
            "cpu": report.get("scheduler", {}).get("cpu_affinity"),
        }, sort_keys=True)
        for case in report.get("cases", []):
            if case.get("status") != 1:
                continue
            host = case.get("host", {})
            if host.get("available"):
                key = (entry["implementation"], case["id"], fingerprint, "host")
                groups.setdefault(key, []).append(host["statistics"]["median_ns"])
            device = case.get("device", {})
            if not device.get("valid"):
                continue
            for core in device.get("core_statistics", []):
                if core.get("count") != device.get("capture", {}).get("sample_count"):
                    continue
                key = (entry["implementation"], case["id"], fingerprint,
                       "device_same_core_envelope", core["device_id"], core["core_x"],
                       core["core_y"], core.get("program_index"))
                groups.setdefault(key, []).append(core["median_cycles"])
    summaries = []
    for key, values in groups.items():
        mean = statistics.mean(values)
        item = {"implementation": key[0], "case_id": key[1],
                "comparison_conditions": json.loads(key[2]), "metric": key[3],
                "units": "ns" if key[3] == "host" else "cycles",
                "independent_submission_count": len(values),
                "within_submission_medians": values,
                "median": statistics.median(values), "min": min(values), "max": max(values),
                "between_submission_cv_percent": 100 * statistics.stdev(values) / mean
                if len(values) > 1 and mean else None}
        if len(key) > 4:
            item["core"] = {"device_id": key[4], "x": key[5], "y": key[6], "program_index": key[7]}
        summaries.append(item)
    return summaries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--problem", required=True)
    parser.add_argument("--language", choices=["ttnn", "metalium-riscv", "metalium-compute"], required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--label", default="local-repeatability-check")
    parser.add_argument("--base", default="http://127.0.0.1:8888")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 3 <= args.repeats <= 30:
        parser.error("--repeats must be between 3 and 30")
    implementations = {"reference": args.reference.read_text()}
    if args.candidate:
        implementations["candidate"] = args.candidate.read_text()
    config = dict(line.split("=", 1) for line in (ROOT / ".env").read_text().splitlines()
                  if "=" in line and not line.startswith("#"))
    client = Client(args.base)
    client.request("/login", {"uname": config["ADMIN_USERNAME"], "password": config["ADMIN_PASSWORD"]})
    experiment_id = uuid.uuid4().hex
    folder = args.output or ROOT / "data/experiments" / experiment_id
    folder.mkdir(parents=True, exist_ok=False, mode=0o700)
    rng = random.Random(args.seed)
    order = []
    for repeat in range(1, args.repeats + 1):
        names = list(implementations)
        rng.shuffle(names)
        order.extend({"repeat": repeat, "implementation": name} for name in names)
    experiment = {
        "schema_version": 1, "experiment_id": experiment_id, "label": args.label,
        "created_at": datetime.now(timezone.utc).isoformat(), "state": "running",
        "problem": args.problem, "language": args.language, "seed": args.seed,
        "source_sha256": {name: hashlib.sha256(code.encode()).hexdigest()
                          for name, code in implementations.items()},
        "repeats_per_implementation": args.repeats, "execution_order": order, "runs": [],
        "eligible_for_ranking": False,
        "notes": ["Each run is a fresh submission with fresh containers per case.",
                  "Reference/candidate order is shuffled within each repeat when both are supplied.",
                  "Aggregate only matching protocol, manifest, images and CPU affinity.",
                  "Within-run medians are retained; no sample outliers are removed.",
                  "Device groups remain separate; no total chip elapsed time is inferred.",
                  "Fresh submissions do not establish statistical independence or eliminate thermal drift."]}
    output = folder / "experiment.json"
    atomic_json(output, experiment)
    try:
        for scheduled in order:
            name = scheduled["implementation"]
            submitted_at = datetime.now(timezone.utc).isoformat()
            rid = client.request(f"/p/{args.problem}/submit", {
                "lang": args.language, "code": implementations[name]})["rid"]
            entry = {**scheduled, "record_id": rid, "submitted_at": submitted_at}
            experiment["runs"].append(entry)
            atomic_json(output, experiment)
            record = client.wait(rid)
            entry["status"] = STATUS[record["status"]]
            entry["report"] = client.request(f"/record/{rid}/profile")
            atomic_json(output, experiment)
            report = entry["report"]
            if (report.get("schema_version") != 2 or report.get("record_id") != rid
                    or report.get("lifecycle", {}).get("state") != "terminal"
                    or not report.get("protocol", {}).get("sha256")
                    or not report.get("environment", {}).get("ttnn_wrapper_sha256")):
                raise RuntimeError("Experiment requires finalized schema-2 reports with protocol and wrapper fingerprints")
            if record["status"] == 1 and (not report.get("cases") or any(
                    not case.get("host", {}).get("available") for case in report["cases"])):
                raise RuntimeError("Accepted submission did not produce complete host timing measurements")
            if record["status"] == 1 and any(
                    case["host"].get("timing_scope") != "host_submit_to_device_complete"
                    or case.get("policy_validation", {}).get("matches") is not True
                    for case in report["cases"]):
                raise RuntimeError("Observed measurements do not match the pinned experiment protocol")
            print(f"Repeat {scheduled['repeat']} {name}: {entry['status']} ({rid})", flush=True)
            if record["status"] != 1:
                raise RuntimeError("An implementation failed correctness; experiment stopped and retained.")
        experiment["state"] = "complete"
    except Exception as error:
        experiment["state"] = "incomplete"
        experiment["error_type"] = type(error).__name__
        raise
    finally:
        experiment["summaries"] = aggregate(experiment["runs"])
        experiment["finished_at"] = datetime.now(timezone.utc).isoformat()
        atomic_json(output, experiment)
        print(f"Experiment saved: {output}", flush=True)


if __name__ == "__main__":
    main()
