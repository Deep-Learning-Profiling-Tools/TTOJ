"""Versioned evaluation policy and restricted, append-per-attempt research data.

Only the explicitly safe report and device CSVs are copied to PROFILES. Code,
hidden tests and process output stay under JOBS, outside the site's mount.
SHA-256 detects accidental changes; this is not signed or write-once storage.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import uuid


POLICY_PATH = Path(__file__).with_name("evaluation-policy.json")
POLICY_SHA256 = "429717d46515f57149cfc53645d17b43403877379feed1a02365f9286b52e0cf"
STATUS_NAMES = {1: "AC", 2: "WA", 3: "TLE", 4: "MLE", 5: "OLE", 6: "RE", 7: "CE", 8: "SE"}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_policy():
    raw = POLICY_PATH.read_bytes()
    if digest(raw) != POLICY_SHA256:
        raise RuntimeError("Evaluation policy differs from its pinned SHA-256; bump policy and pin before deployment")
    return raw, json.loads(raw)


def atomic_write(path: Path, data: bytes, mode=0o600):
    """Same-directory replace and fsync; readers never see partial JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temporary, "xb") as output:
            os.chmod(temporary, mode)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()


class Attempt:
    """An attempt starts before manifest loading or compilation can fail."""
    def __init__(self, jobs: Path, profiles: Path, task: dict):
        rid = str(task.get("rid", ""))
        if not re.fullmatch(r"[0-9a-fA-F]{24}", rid):
            raise ValueError("Invalid record id: expected 24 hexadecimal characters")
        self.rid = rid.lower()
        self.id = uuid.uuid4().hex
        self.private = jobs / self.rid / "attempts" / self.id
        self.public = profiles / self.rid / "attempts" / self.id
        self.latest = profiles / self.rid
        self.private.mkdir(parents=True, mode=0o700)
        # Historical /jobs/<rid> directories are kept. The new attempt subtree
        # is private even if an older record directory is world traversable.
        os.chmod(self.private.parent, 0o700)
        os.chmod(self.private, 0o700)
        self.public.mkdir(parents=True, mode=0o755)
        self.lock = threading.RLock()
        self.finalized = False
        self.publish_latest = True
        self.sequence = 0
        self.policy_raw, self.policy = load_policy()
        self.received_at, self.received_ns = utc_now(), time.monotonic_ns()
        identity = {"record_id": self.rid, "attempt_id": self.id,
                    "domain_id": task.get("domainId"), "problem_id": task.get("pid"),
                    "language": task.get("lang"), "priority": task.get("priority"),
                    "domainId": task.get("domainId"), "pid": task.get("pid"), "lang": task.get("lang"),
                    "code_text_provided": isinstance(task.get("code"), str),
                    "code_attachment_provided": bool(task.get("files", {}).get("code")),
                    "self_test": bool(task.get("input"))}
        # No account lookup and no fabricated participant identifier.
        if isinstance(task.get("uid"), int) and not isinstance(task.get("uid"), bool):
            identity["participant_id"] = task["uid"]
            identity["uid"] = task["uid"]
        source = task.get("code") if isinstance(task.get("code"), str) else ""
        identity["source_sha256"] = digest(source.encode())
        self.write_private("task.json", json_bytes(identity))
        self.source_name = "submission.py" if task.get("lang") == "ttnn" else "solution.cpp"
        self.write_private(self.source_name, source.encode())
        if task.get("input"):
            self.write_private("self-test-input.json", json_bytes(task["input"]))
        self.write_private("policy.json", self.policy_raw)
        self.report = {
            "schema_version": 2, "record_id": self.rid, "attempt_id": self.id,
            "created_at": self.received_at,
            "protocol": {"id": self.policy["id"], "sha256": POLICY_SHA256, "policy": self.policy},
            "lifecycle": {"state": "running", "received_at": self.received_at,
                          "received_monotonic_ns": self.received_ns, "finished_at": None,
                          "finished_monotonic_ns": None, "queue_wait_ms": None,
                          "queue_time_note": "Hydro enqueue timestamp is not supplied; broker receipt is recorded"},
            "environment": {"source_sha256": digest(source.encode())},
            "cases": [],
            "quality": {"eligible_for_ranking": False,
                        "reason_codes": self.policy["ranking_blockers"] + ["attempt_not_finalized"],
                        "warnings": [], "purpose": self.policy["purpose"]},
            "data_recording": {"private_archive": "source, manifest, task identity, lifecycle events and raw process output",
                               "public_artifacts": "report.json, case-N-device.csv and case-N-counters.csv only",
                               "integrity": "SHA-256 inventory; operator-writable, not signed or write-once",
                               "retention": "indefinite_until_manual_administrator_removal"},
        }
        self.event("attempt_received", problem_id=task.get("pid"), language=task.get("lang"))
        self.checkpoint()

    def write_private(self, name: str, data: bytes):
        if self.finalized:
            raise RuntimeError("Attempt is finalized")
        path = self.private / name
        if path.parent != self.private:
            raise ValueError("Private artifact must have a flat filename")
        atomic_write(path, data)

    def event(self, kind: str, **fields):
        with self.lock:
            if self.finalized:
                raise RuntimeError("Attempt is finalized")
            self.sequence += 1
            value = {"sequence": self.sequence, "event": kind, "utc": utc_now(),
                     "monotonic_ns": time.monotonic_ns(), **fields}
            path = self.private / "events.jsonl"
            with open(path, "ab") as output:
                os.chmod(path, 0o600)
                output.write((json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n").encode())
                output.flush()
                os.fsync(output.fileno())

    def checkpoint(self):
        with self.lock:
            if self.finalized:
                raise RuntimeError("Attempt is finalized")
            data = json_bytes(self.report)
            atomic_write(self.private / "report.json", data)
            atomic_write(self.public / "report.json", data, 0o644)
            if not self.publish_latest:
                return
            # Remove stale legacy CSVs when a new attempt takes over latest.
            latest_id = None
            try:
                latest_id = json.loads((self.latest / "latest.json").read_text()).get("attempt_id")
            except (OSError, ValueError):
                pass
            if latest_id != self.id:
                for stale in self.latest.glob("case-*-*.csv"):
                    stale.unlink()
            atomic_write(self.latest / "report.json", data, 0o644)
            for artifact in self.public.glob("case-*-*.csv"):
                atomic_write(self.latest / artifact.name, artifact.read_bytes(), 0o644)
            atomic_write(self.latest / "latest.json", json_bytes({"schema_version": 2,
                "attempt_id": self.id, "record_id": self.rid, "state": self.report["lifecycle"]["state"]}), 0o644)

    def set_manifest(self, raw: bytes, manifest: dict):
        self.write_private("manifest.json", raw)
        self.report["environment"]["manifest_sha256"] = digest(raw)
        self.report["environment"]["manifest_canonical_sha256"] = digest(json.dumps(manifest, sort_keys=True).encode())
        tolerances = {name: manifest.get(name, 0) for name in ("atol", "rtol")}
        tolerances_valid = all(isinstance(value, (int, float)) and not isinstance(value, bool)
                               and math.isfinite(value) and value >= 0 for value in tolerances.values())
        self.report["correctness_policy"] = {**(tolerances if tolerances_valid else {}),
            "tolerances_valid": tolerances_valid,
            "reference": "trusted manifest outputs retained only in restricted archive",
            "comparison": self.policy["correctness"]["comparison"]}
        tests = manifest.get("tests")
        self.event("manifest_loaded", sha256=digest(raw), test_count=len(tests) if isinstance(tests, list) else None)
        self.checkpoint()

    def write_csv(self, case_id: int, csv_text: str, kind: str = "device"):
        name = f"case-{case_id}-{kind}.csv"
        raw = csv_text.encode()
        self.write_private(name, raw)
        atomic_write(self.public / name, raw, 0o644)
        return name

    def capture(self, case_id: int, pass_name: str, execution, outcome=None):
        """Raw bytes are bounded by OUTPUT_LIMIT, including failure captures."""
        with self.lock:
            prefix = f"case-{case_id}-{pass_name}"
            self.write_private(f"{prefix}.stdout", bytes(execution.out))
            self.write_private(f"{prefix}.stderr", bytes(execution.err))
            metadata = {"case_id": case_id, "pass": pass_name,
                "container_id": execution.container.id,
                "output_truncated": bool(execution.output_exceeded),
                "output_limit_bytes": 262144, "peak_host_memory_bytes": execution.peak_memory,
                "reader_error": execution.thread_error}
            if outcome:
                metadata.update({key: outcome.get(key) for key in ("status", "time", "memory", "exit_code", "telemetry")})
            self.write_private(f"{prefix}.json", json_bytes(metadata))

    def finish(self, final: dict, reason: str, quality: dict):
        with self.lock:
            if self.finalized:
                return
            status = final.get("status")
            if status is None:
                quality["reason_codes"] = list(dict.fromkeys(quality.get("reason_codes", []) + ["interrupted_attempt"]))
            self.report["final"] = {**final, "status_name": STATUS_NAMES.get(status, "CANCELLED")}
            self.report["quality"] = quality
            self.report["lifecycle"].update({"state": "terminal", "finished_at": utc_now(),
                "finished_monotonic_ns": time.monotonic_ns(), "terminal_reason": reason})
            self.event("attempt_terminal", status=status, status_name=self.report["final"]["status_name"], reason=reason)
            self.write_private("result.json", json_bytes({**final, "cases": self.report["cases"]}))
            self.checkpoint()
            self.write_inventory()
            # Files are read-only after completion, and the archive remains
            # root-only. An administrator/root can still modify them.
            for path in self.private.rglob("*"):
                if path.is_file():
                    os.chmod(path, 0o400)
                elif path.is_dir():
                    os.chmod(path, 0o700)
            for path in self.public.iterdir():
                if path.is_file():
                    os.chmod(path, 0o444)
            self.finalized = True

    def write_inventory(self):
        def records(root):
            return [{"path": str(path.relative_to(root)), "sha256": digest(path.read_bytes()),
                     "size": path.stat().st_size} for path in sorted(root.rglob("*"))
                    if path.is_file() and path.name != "inventory.json" and not path.name.endswith(".tmp")]
        value = {"schema_version": 1, "record_id": self.rid, "attempt_id": self.id,
                 "finalized_at": utc_now(), "excludes": ["inventory.json"],
                 "files": {"private": records(self.private), "public": records(self.public)}}
        self.write_private("inventory.json", json_bytes(value))


def recover_interrupted(jobs: Path, profiles: Path):
    """Record observed restart recovery without inventing a shutdown time."""
    recovered = 0
    for path in jobs.glob("[0-9a-f]*/attempts/*/report.json"):
        try:
            report = json.loads(path.read_text())
            if report.get("schema_version") != 2 or report.get("lifecycle", {}).get("state") != "running":
                continue
            rid, attempt_id = report["record_id"], report["attempt_id"]
            if not re.fullmatch(r"[0-9a-f]{24}", rid) or not re.fullmatch(r"[0-9a-f]{32}", attempt_id):
                continue
            attempt = object.__new__(Attempt)
            attempt.private, attempt.public, attempt.latest = path.parent, profiles / rid / "attempts" / attempt_id, profiles / rid
            attempt.rid, attempt.id, attempt.report = rid, attempt_id, report
            attempt.lock, attempt.finalized = threading.RLock(), False
            try:
                latest_id = json.loads((attempt.latest / "latest.json").read_text()).get("attempt_id")
            except (OSError, ValueError):
                latest_id = attempt_id
            attempt.publish_latest = latest_id == attempt_id
            lines = (path.parent / "events.jsonl").read_text().splitlines()
            valid_events = []
            for line in lines:
                try:
                    valid_events.append(json.loads(line))
                except ValueError:
                    # Retain evidence of the truncated append separately.
                    atomic_write(path.parent / "events-truncated-recovery.txt", line.encode())
            atomic_write(path.parent / "events.jsonl", b"".join((json.dumps(event, ensure_ascii=False) + "\n").encode() for event in valid_events))
            attempt.sequence = max((event.get("sequence", 0) for event in valid_events), default=0)
            for case in report.get("cases", []):
                if case.get("state") == "running":
                    case.update({"state": "interrupted", "interruption_reason": "worker_restart_observed"})
            attempt.event("restart_recovery_observed", stopped_at_unknown=True)
            quality = report.get("quality", {})
            quality.update({"eligible_for_ranking": False,
                "reason_codes": list(dict.fromkeys(quality.get("reason_codes", []) + ["interrupted_attempt"]))})
            attempt.finish({"status": None, "score": 0, "message": "Worker restarted before terminal persistence; interruption observed at recovery."},
                           "worker_restart_observed_stop_time_unknown", quality)
            recovered += 1
        except (OSError, ValueError, KeyError):
            # A malformed historical record cannot be repaired by inventing data.
            continue
    return recovered
