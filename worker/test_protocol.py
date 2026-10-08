"""Meaningful protocol checks without GPU work or a Docker socket.

Run in the broker image with the current worker files mounted read-only.
Tests cover pre-case failures, all verdicts, rejudge preservation, interruption,
private/public separation, inventory integrity and cleanup under storage faults.
"""
import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("JOBS_HOST_DIR", "/unused")
os.environ.setdefault("TTNN_IMAGE", "fake-ttnn-tag")
import worker
from protocol import Attempt, POLICY_SHA256, digest, load_policy, recover_interrupted


TASK = {"rid": "ab" * 12, "domainId": "system", "pid": 1, "lang": "ttnn", "uid": 42,
        "code": "def solve(a, b):\n    return a\n", "priority": 0}
MANIFEST = {"mode": "ttnn", "language": "ttnn", "tests": [{"input": "hidden_input", "output": "1"}],
            "atol": 0, "rtol": 0, "time_ms": 1000}


def outcome(status=None, actual="1"):
    profile = {"samples_ns": list(range(100, 150)), "warmup": 5,
               "repeatability_verified": True, "timing_scope": "host_submit_to_device_complete"}
    return {"status": status, "time": 3.5, "memory": 10,
            "stdout": f"TT_OJ_RESULT:{actual}\nTT_OJ_PROFILE:{json.dumps(profile)}\n",
            "stderr": "private_process_diagnostic", "exit_code": 0 if status is None else 1,
            "telemetry": [], "profiler_csv": None}


class FakeExecution:
    def __init__(self, value, attempt, case_id=1, delay=0):
        self.value, self.delay = value, delay
        self.attempt, self.case_id, self.pass_name = attempt, case_id, "host"
        self.container = SimpleNamespace(id="fake-container")
        self.out, self.err = bytearray(), bytearray()
        self.output_exceeded, self.peak_memory, self.thread_error = False, 10000, None
        self.removed, self.stopped = False, False

    def run(self, timeout):
        self.attempt.event("container_started_observed", case_id=self.case_id, pass_name=self.pass_name)
        time.sleep(self.delay)
        if isinstance(self.value, Exception):
            raise self.value
        self.out.extend(self.value["stdout"].encode())
        self.err.extend(self.value["stderr"].encode())
        self.attempt.event("container_exit_observed", case_id=self.case_id, pass_name=self.pass_name)
        return self.value

    def stop(self):
        self.stopped = True

    def remove(self):
        self.removed = True


class FakeJudge(worker.Judge):
    def __init__(self, values=None, manifest_error=None, preflight_error=None, delay=0):
        self.policy = load_policy()[1]
        self.active, self.attempt = None, None
        self.messages, self.executions = [], []
        self.values = values if values is not None else [outcome()]
        self.manifest_error, self.preflight_error, self.delay = manifest_error, preflight_error, delay
        images = SimpleNamespace(get=lambda tag: SimpleNamespace(id="sha256:" + tag, attrs={"RepoDigests": []}))
        containers = SimpleNamespace(get=lambda hostname: SimpleNamespace(image=SimpleNamespace(id="worker-image")))
        self.docker = SimpleNamespace(images=images, containers=containers)

    async def manifest(self, task):
        if self.manifest_error:
            raise self.manifest_error
        return copy.deepcopy(MANIFEST), json.dumps(MANIFEST).encode()

    async def wait_for_hardware(self, rid, timeout=120):
        if self.preflight_error:
            raise self.preflight_error
        return {"thermal_preflight": "pass", "known_external_containers": [], "wait_ms": 0}

    async def send(self, rid, key, **payload):
        self.messages.append({"rid": rid, "key": key, **payload})

    async def prepare_execution(self, folder, task, manifest, case, profiling=False):
        execution = FakeExecution(self.values.pop(0), self.attempt, delay=self.delay)
        self.executions.append(execution)
        return execution


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.jobs, self.profiles = Path(self.temp.name) / "jobs", Path(self.temp.name) / "profiles"
        self.jobs.mkdir()
        self.profiles.mkdir()
        self.patches = [patch.object(worker, "JOBS", self.jobs), patch.object(worker, "PROFILES", self.profiles),
                        patch.object(worker, "DEVICE_PROFILER_SUPPORTED", False),
                        patch.object(worker, "telemetry_snapshot", return_value={"devices": []})]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        # Final archives are read-only by convention; temp teardown owns them.
        for path in Path(self.temp.name).rglob("*"):
            if path.is_file():
                path.chmod(0o600)
        self.temp.cleanup()

    def report(self):
        return json.loads((self.profiles / TASK["rid"] / "report.json").read_text())

    def private(self, report=None):
        report = report or self.report()
        return self.jobs / TASK["rid"] / "attempts" / report["attempt_id"]

    def verify(self, report=None):
        report = report or self.report()
        private = self.private(report)
        inventory = json.loads((private / "inventory.json").read_text())
        for visibility, base in (("private", private), ("public", self.profiles / TASK["rid"] / "attempts" / report["attempt_id"])):
            for entry in inventory["files"][visibility]:
                raw = (base / entry["path"]).read_bytes()
                self.assertEqual(entry["sha256"], digest(raw))
                self.assertEqual(entry["size"], len(raw))
        self.assertEqual(report["lifecycle"]["state"], "terminal")
        self.assertFalse(report["quality"]["eligible_for_ranking"])
        self.assertEqual(report["protocol"]["sha256"], POLICY_SHA256)
        events = [json.loads(line) for line in (private / "events.jsonl").read_text().splitlines()]
        self.assertEqual([entry["sequence"] for entry in events], list(range(1, len(events) + 1)))
        self.assertEqual(events[-1]["event"], "attempt_terminal")
        self.assertTrue(all("utc" in entry and "monotonic_ns" in entry for entry in events))

    async def test_all_case_verdicts_persist(self):
        for expected, value in [(worker.AC, outcome()), (worker.WA, outcome(actual="2")),
                                (worker.RE, outcome(worker.RE)), (worker.TLE, outcome(worker.TLE)),
                                (worker.MLE, outcome(worker.MLE)), (worker.OLE, outcome(worker.OLE))]:
            with self.subTest(status=expected):
                judge = FakeJudge([value])
                await judge.judge(TASK)
                report = self.report()
                self.assertEqual(report["final"]["status"], expected)
                self.assertEqual(report["cases"][0]["state"], "terminal")
                self.assertTrue(judge.executions[0].removed)
                self.verify(report)

    async def test_syntax_failure_preserves_source_before_any_case(self):
        task = {**TASK, "code": "SECRET_SOURCE_SENTINEL = ("}
        await FakeJudge().judge(task)
        report = self.report()
        self.assertEqual(report["final"]["status"], worker.CE)
        self.assertEqual(report["cases"], [])
        self.assertEqual((self.private() / "submission.py").read_text(), task["code"])
        self.assertNotIn("SECRET_SOURCE_SENTINEL", json.dumps(report))
        self.assertNotIn("compilerText", report["final"])
        self.verify()

    async def test_manifest_failure_is_terminal_without_fabricated_reference(self):
        await FakeJudge(manifest_error=RuntimeError("PRIVATE_FETCH_ERROR")).judge(TASK)
        report = self.report()
        self.assertEqual(report["final"]["status"], worker.SE)
        self.assertNotIn("manifest_sha256", report["environment"])
        self.assertNotIn("PRIVATE_FETCH_ERROR", json.dumps(report))
        self.assertEqual((self.private() / "submission.py").read_text(), TASK["code"])
        self.verify()

    async def test_preflight_failure_is_terminal_with_reference(self):
        await FakeJudge(preflight_error=RuntimeError("PRIVATE_PREFLIGHT_ERROR")).judge(TASK)
        report = self.report()
        self.assertEqual(report["final"]["status"], worker.SE)
        self.assertEqual(report["environment"]["manifest_sha256"], digest((self.private() / "manifest.json").read_bytes()))
        self.assertEqual(report["cases"], [])
        self.assertNotIn("PRIVATE_PREFLIGHT_ERROR", json.dumps(report))
        self.verify()

    async def test_cancel_persists_and_removes(self):
        judge = FakeJudge(delay=0.15)
        pending = asyncio.create_task(judge.judge(TASK))
        while not judge.executions:
            await asyncio.sleep(0.001)
        await asyncio.sleep(0.01)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        report = self.report()
        self.assertEqual(report["final"]["status_name"], "CANCELLED")
        self.assertEqual(report["cases"][0]["state"], "interrupted")
        self.assertTrue(judge.executions[0].removed)
        self.assertTrue(judge.executions[0].stopped)
        self.verify()

    async def test_rejudge_preserves_previous_attempt_and_stale_csv_not_latest(self):
        first = Attempt(self.jobs, self.profiles, TASK)
        first.write_csv(1, "fixture_csv")
        first.finish({"status": worker.AC, "score": 100}, "test", {"eligible_for_ranking": False})
        previous = (first.private / "report.json").read_bytes()
        await FakeJudge().judge(TASK)
        self.assertNotEqual(first.id, self.report()["attempt_id"])
        self.assertEqual((first.private / "report.json").read_bytes(), previous)
        self.assertTrue((first.public / "case-1-device.csv").exists())
        self.assertFalse((first.latest / "case-1-device.csv").exists())
        self.verify()

    async def test_private_identity_reference_logs_and_inventory(self):
        await FakeJudge().judge(TASK)
        private, report = self.private(), self.report()
        self.assertEqual(private.stat().st_mode & 0o777, 0o700)
        self.assertEqual(private.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(json.loads((private / "task.json").read_text())["uid"], 42)
        for forbidden in ("hidden_input", "private_process_diagnostic", "participant_id", '"uid"'):
            self.assertNotIn(forbidden, json.dumps(report))
        self.assertEqual((private / "case-1-host.stderr").read_text(), "private_process_diagnostic")
        self.assertEqual((private / "submission.py").stat().st_mode & 0o777, 0o400)
        public_names = {path.name for path in (self.profiles / TASK["rid"] / "attempts" / report["attempt_id"]).iterdir()}
        self.assertEqual(public_names, {"report.json"})
        self.verify()

    async def test_recovery_preserves_newer_latest_and_truncated_event(self):
        old = Attempt(self.jobs, self.profiles, TASK)
        with open(old.private / "events.jsonl", "ab") as output:
            output.write(b'{"partial":')
        await FakeJudge().judge(TASK)
        newest_id = self.report()["attempt_id"]
        self.assertEqual(recover_interrupted(self.jobs, self.profiles), 1)
        self.assertEqual(self.report()["attempt_id"], newest_id)
        recovered = json.loads((old.private / "report.json").read_text())
        self.assertEqual(recovered["lifecycle"]["terminal_reason"], "worker_restart_observed_stop_time_unknown")
        self.assertTrue((old.private / "events-truncated-recovery.txt").exists())
        self.verify(recovered)

    async def test_capture_write_failure_still_removes(self):
        attempt = Attempt(self.jobs, self.profiles, TASK)
        execution = FakeExecution(outcome(), attempt)
        with patch.object(attempt, "capture", side_effect=OSError("storage failure")):
            with self.assertRaises(OSError):
                await FakeJudge().run_execution(execution, 1)
        self.assertTrue(execution.removed)

    async def test_stop_journal_failure_still_kills(self):
        container = SimpleNamespace(id="fake", kill=unittest.mock.Mock())
        recorder = SimpleNamespace(event=unittest.mock.Mock(side_effect=OSError("storage failure")))
        execution = worker.Execution(None, container, attempt=recorder)
        execution.stop()
        container.kill.assert_called_once()

    async def test_create_journal_failure_still_removes(self):
        container = SimpleNamespace(id="fake", remove=unittest.mock.Mock())
        judge = FakeJudge()
        judge.docker = SimpleNamespace(containers=SimpleNamespace(create=unittest.mock.Mock(return_value=container)))
        judge.attempt = SimpleNamespace(id="fake-attempt", event=unittest.mock.Mock(side_effect=OSError("storage failure")))
        folder = self.jobs / "case" / "1"
        manifest = {"mode": "metalium-riscv"}
        with self.assertRaises(OSError):
            judge.prepare(folder, {**TASK, "code": "// source"}, manifest, {"input": "1 2"})
        container.remove.assert_called_once_with(force=True)

    async def test_recipe_mismatch_explicitly_disqualifies(self):
        value = outcome()
        marker = json.loads(value["stdout"].split("TT_OJ_PROFILE:")[1])
        marker["warmup"] = 4
        value["stdout"] = "TT_OJ_RESULT:1\nTT_OJ_PROFILE:" + json.dumps(marker) + "\n"
        await FakeJudge([value]).judge(TASK)
        self.assertFalse(self.report()["cases"][0]["policy_validation"]["matches"])
        self.assertIn("measurement_recipe_mismatch", self.report()["quality"]["reason_codes"])
        self.verify()

    async def test_arbitrary_profile_text_is_restricted_to_raw_log(self):
        value = outcome()
        marker = json.loads(value["stdout"].split("TT_OJ_PROFILE:")[1])
        marker.update({"source": "PRIVATE_SOURCE_SENTINEL", "scope_detail": "PRIVATE_SOURCE_SENTINEL", "warmup_count": "PRIVATE_SOURCE_SENTINEL"})
        value["stdout"] = "TT_OJ_RESULT:1\nTT_OJ_PROFILE:" + json.dumps(marker) + "\n"
        await FakeJudge([value]).judge(TASK)
        self.assertNotIn("PRIVATE_SOURCE_SENTINEL", json.dumps(self.report()))
        self.assertIn("PRIVATE_SOURCE_SENTINEL", (self.private() / "case-1-host.stdout").read_text())
        self.verify()

    async def test_unsafe_record_id_cannot_create_path(self):
        await FakeJudge().judge({**TASK, "rid": "../../outside"})
        self.assertEqual(list(self.jobs.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
