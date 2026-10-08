"""Verify attempt archives and export pseudonymized metrics using only stdlib.

Run locally as an administrator. Exports exclude source, inputs, answers, logs,
usernames, and emails. Pseudonyms still require review before public release.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import stat

ROOT = Path(__file__).resolve().parents[1]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(path.read_text())


def checked_path(root, relative):
    path = root / relative
    if not isinstance(relative, str) or path.is_symlink() or root.resolve() not in path.resolve().parents:
        raise ValueError("Unsafe artifact path")
    return path


def verify_attempt(folder, profiles):
    report = read_json(folder / "report.json")
    rid, attempt_id = folder.parent.parent.name, folder.name
    if report.get("schema_version") != 2 or report.get("record_id") != rid or report.get("attempt_id") != attempt_id:
        raise ValueError("Attempt identity/schema mismatch")
    if report.get("lifecycle", {}).get("state") != "terminal":
        raise ValueError("Attempt has not finalized")
    if stat.S_IMODE(folder.stat().st_mode) & 0o077:
        raise ValueError("Private attempt directory is accessible to other users")
    inventory = read_json(folder / "inventory.json")
    if (inventory.get("schema_version") != 1 or inventory.get("record_id") != rid
            or inventory.get("attempt_id") != attempt_id):
        raise ValueError("Inventory identity/schema mismatch")
    public_folder = profiles / rid / "attempts" / attempt_id
    verified = 0
    for namespace, base in (("private", folder), ("public", public_folder)):
        items = inventory["files"][namespace]
        if not items:
            raise ValueError("Missing archive inventory entries")
        paths = [item["path"] for item in items]
        actual = {str(path.relative_to(base)) for path in base.rglob("*")
                  if path.is_file() and path.name != "inventory.json" and not path.name.endswith(".tmp")}
        if len(paths) != len(set(paths)) or set(paths) != actual:
            raise ValueError("Inventory does not exactly cover archive files")
        if namespace == "private":
            required = {"report.json", "task.json", "policy.json", "events.jsonl", "result.json"}
            if not ({"submission.py", "solution.cpp"} & set(paths)):
                raise ValueError("Source snapshot is absent from inventory")
            if report.get("environment", {}).get("manifest_sha256"):
                required.add("manifest.json")
            if not required <= set(paths):
                raise ValueError("Required private artifact is absent from inventory")
        for item in items:
            payload = checked_path(base, item["path"]).read_bytes()
            if digest(payload) != item["sha256"] or len(payload) != item["size"]:
                raise ValueError(f"Artifact checksum mismatch: {namespace}/{item['path']}")
            verified += 1
    policy_payload = (folder / "policy.json").read_bytes()
    if digest(policy_payload) != report["protocol"]["sha256"]:
        raise ValueError("Policy fingerprint mismatch")
    policy = json.loads(policy_payload)
    if policy != report["protocol"]["policy"]:
        raise ValueError("Reported policy differs from archived policy")
    source_name = "submission.py" if (folder / "submission.py").exists() else "solution.cpp"
    if digest((folder / source_name).read_bytes()) != report["environment"]["source_sha256"]:
        raise ValueError("Source fingerprint differs from archive")
    manifest_sha = report["environment"].get("manifest_sha256")
    if manifest_sha and digest((folder / "manifest.json").read_bytes()) != manifest_sha:
        raise ValueError("Manifest fingerprint differs from archive")
    if (public_folder / "report.json").read_bytes() != (folder / "report.json").read_bytes():
        raise ValueError("Public/private reports differ")
    for case in report.get("cases", []):
        host = case.get("host", {})
        if host.get("available"):
            if len(host["samples_ns"]) != host["sample_count"] or host["sample_count"] != policy["host"]["sample_count"]:
                raise ValueError("Host sample count differs from protocol")
            if host["warmup_count"] != policy["host"]["warmup_count"]:
                raise ValueError("Host warmup count differs from protocol")
        device = case.get("device", {})
        if device.get("valid"):
            if device["capture"]["sample_count"] != policy["device"]["sample_count"]:
                raise ValueError("Device sample count differs from protocol")
            if device["capture"].get("warmup_count", device["capture"].get("warmup")) != policy["device"]["warmup_count"]:
                raise ValueError("Device warmup count differs from protocol")
            for filename in device.get("artifacts", []):
                if digest(checked_path(public_folder, filename).read_bytes()) != device["artifact_sha256"]:
                    raise ValueError("Device CSV checksum mismatch")
    events = [json.loads(line) for line in (folder / "events.jsonl").read_text().splitlines()]
    if not events or events[0]["event"] != "attempt_received" or events[-1]["event"] != "attempt_terminal":
        raise ValueError("Lifecycle does not contain receipt and terminal events")
    if [event["sequence"] for event in events] != list(range(1, len(events) + 1)):
        raise ValueError("Lifecycle event sequence is incomplete")
    return report, {"record_id": rid, "attempt_id": attempt_id,
                    "status": report.get("final", {}).get("status_name"),
                    "verified_artifacts": verified, "event_count": len(events)}


def key_bytes(path):
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(os.urandom(32))
    if not path.is_file() or path.is_symlink() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("Pseudonym key must be a private regular file (mode 0600)")
    key = path.read_bytes()
    if len(key) < 32:
        raise ValueError("Pseudonym key must contain at least 32 bytes")
    return key


def pseudonym(key, namespace, value):
    if value is None:
        return None
    return hmac.new(key, f"{namespace}:{value}".encode(), hashlib.sha256).hexdigest()


def numeric_fields(value, names):
    result = {}
    for name in names:
        item = value.get(name)
        if item is None or isinstance(item, bool):
            result[name] = item
        elif isinstance(item, (int, float)) and math.isfinite(item):
            result[name] = item
        elif isinstance(item, list) and all(type(x) in (int, float) and math.isfinite(x) for x in item):
            result[name] = item
    return result


def safe_statistics(items, key):
    names = ("device_id", "core_x", "core_y", "program_index", "count", "complete", "expected_sample_count",
             "samples_cycles", "min_cycles", "median_cycles", "p95_cycles", "max_cycles", "mean_cycles", "stdev_cycles",
             "min_ns", "median_ns", "p95_ns", "max_ns", "mean_ns", "stdev_ns")
    result = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        group = numeric_fields(item, names)
        for name in ("zone", "risc"):
            if item.get(name) is not None:
                group[f"{name}_pseudonym"] = pseudonym(key, name, item[name])
        result.append(group)
    return result


def known_reason(value):
    allowed = {"no_profile_produced", "invalid_profile_json", "invalid_or_missing_samples",
               "profile_is_not_an_object", "profiler_enabled_sdk_required",
               "device_profiling_failed_or_no_csv"}
    return value if value is None or value in allowed else "other_redacted"


def export_row(report, task, key):
    domain = task.get("domainId", task.get("domain_id"))
    uid = task.get("uid", task.get("participant_id"))
    environment = report.get("environment", {})
    row = {
        "schema_version": 1,
        "record_pseudonym": pseudonym(key, "record", report["record_id"]),
        "attempt_pseudonym": pseudonym(key, "attempt", report["attempt_id"]),
        "participant_pseudonym": pseudonym(key, "participant", f"{domain}:{uid}") if uid is not None else None,
        "participant_identity_available": uid is not None,
        "problem_id": task.get("pid", task.get("problem_id")),
        "language": task.get("lang", task.get("language")),
        "protocol": {name: report["protocol"].get(name) for name in ("id", "sha256")},
        "source_sha256": environment.get("source_sha256"),
        "manifest_sha256": environment.get("manifest_sha256"),
        "sdk_commit": environment.get("sdk_commit"), "image_ids": environment.get("image_ids"),
        "wrapper_sha256": environment.get("ttnn_wrapper_sha256"),
        "worker_code_sha256": environment.get("worker_code_sha256"),
        "status": report.get("final", {}).get("status_name"),
        "score": report.get("final", {}).get("score"),
        "terminal_reason": report.get("lifecycle", {}).get("terminal_reason"),
        "eligible_for_ranking": report.get("quality", {}).get("eligible_for_ranking", False),
        "cases": [],
    }
    for case in report.get("cases", []):
        host, device = case.get("host", {}), case.get("device", {})
        host_view = numeric_fields(host, ("warmup_count", "sample_count", "samples_ns", "repeatability_verified"))
        host_view.update({"available": host.get("available") is True, "reason": known_reason(host.get("reason")),
                          "timing_scope": "host_submit_to_device_complete" if host.get("timing_scope") == "host_submit_to_device_complete" else "other_redacted",
                          "statistics": numeric_fields(host.get("statistics", {}),
                              ("min_ns", "median_ns", "mean_ns", "p95_ns", "max_ns", "mad_ns", "stdev_ns", "cv_percent", "median_ci95_ns"))})
        device_view = numeric_fields(device, ("clock_mhz",))
        device_view.update({"available": device.get("available") is True, "valid": device.get("valid") is True,
                            "instrumented": device.get("instrumented") is True, "reason": known_reason(device.get("reason")),
                            "statistics": safe_statistics(device.get("statistics"), key),
                            "core_statistics": safe_statistics(device.get("core_statistics"), key)})
        row["cases"].append({
            "id": case["id"], "status": case.get("status"), "wall_time_ms": case.get("wall_time_ms"),
            "correctness": numeric_fields(case.get("correctness", {}),
                ("reference_available", "atol", "rtol", "host_repeatability_verified", "device_repeatability_verified")),
            "host": host_view, "device": device_view,
        })
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["audit", "export"])
    parser.add_argument("--jobs", type=Path, default=ROOT / "data/jobs")
    parser.add_argument("--profiles", type=Path, default=ROOT / "data/profiles")
    parser.add_argument("--records", nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, help="Required for export; generated privately if absent")
    args = parser.parse_args()
    if args.records and any(not re.fullmatch(r"[a-f0-9]{24}", rid) for rid in args.records):
        parser.error("Record IDs must be 24 lowercase hexadecimal characters")
    if args.action == "export" and not args.key_file:
        parser.error("export requires --key-file")
    folders = sorted(args.jobs.glob("*/attempts/*"))
    if args.records:
        folders = [folder for folder in folders if folder.parent.parent.name in args.records]
    verified, rows, unfinished = [], [], 0
    key = key_bytes(args.key_file) if args.action == "export" else None
    for folder in folders:
        report = read_json(folder / "report.json")
        if report.get("lifecycle", {}).get("state") != "terminal":
            unfinished += 1
            continue
        report, result = verify_attempt(folder, args.profiles)
        verified.append(result)
        if key:
            rows.append(export_row(report, read_json(folder / "task.json"), key))
    if not verified:
        raise ValueError("No finalized schema-2 attempts found; legacy reports cannot be retroactively reconstructed")
    if args.records and set(args.records) - {item["record_id"] for item in verified}:
        raise ValueError("Some requested records have no finalized schema-2 attempts")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        if args.action == "export":
            for row in rows:
                output.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
        else:
            json.dump({"checked_at": datetime.now(timezone.utc).isoformat(),
                       "unfinished_attempts_skipped": unfinished, "attempts": verified}, output, indent=2)
    print(json.dumps({"action": args.action, "verified_attempts": len(verified),
                      "unfinished_attempts_skipped": unfinished, "output": str(args.output)}))


if __name__ == "__main__":
    main()
