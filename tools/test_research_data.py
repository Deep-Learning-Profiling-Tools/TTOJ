"""Focused regressions for archive integrity, metrics privacy and grouping.

All attempts are stdlib fixtures in temporary directories. No device, Docker,
network, credentials or live OJ data are used.
"""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load_local(name, relative):
    specification = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


# run_experiment imports its sibling check_e2e. Restrict the temporary search
# path to this repository's tools directory, then restore the caller's path.
original_path = sys.path[:]
try:
    sys.path.insert(0, str(ROOT / "tools"))
    protocol = load_local("tt_oj_test_protocol", "worker/protocol.py")
    research = load_local("tt_oj_test_research_data", "tools/research_data.py")
    experiment = load_local("tt_oj_test_run_experiment", "tools/run_experiment.py")
finally:
    sys.path[:] = original_path


class ResearchDataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tt-oj-research-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.profiles = self.root / "profiles"
        self.attempt = protocol.Attempt(self.root / "jobs", self.profiles, {
            "rid": "a" * 24, "domainId": "test-domain", "pid": 1,
            "lang": "ttnn", "uid": 7, "code": "# PRIVATE_SOURCE\ndef solve(a,b): return a\n",
        })
        manifest = {"mode": "ttnn", "language": "ttnn", "atol": 0,
                    "rtol": 0, "tests": [{"input": "PRIVATE_INPUT", "output": "PRIVATE_ANSWER"}]}
        self.attempt.set_manifest(protocol.json_bytes(manifest), manifest)
        self.attempt.report["environment"].update({
            "ttnn_wrapper_sha256": "wrapper-v1", "image_ids": {"ttnn": "fixed-image"},
            "before": {"driver_version": "test-driver", "devices": [], "cpu_governors": {}},
        })
        self.attempt.report["scheduler"] = {"cpu_affinity": "4-7"}
        host_count = self.attempt.policy["host"]["sample_count"]
        device_count = self.attempt.policy["device"]["sample_count"]
        csv_text = "fixture device csv\n"
        artifact = self.attempt.write_csv(1, csv_text)
        self.attempt.report["cases"] = [{
            "id": 1, "status": 1, "wall_time_ms": 10,
            "host": {"available": True, "timing_scope": "host_submit_to_device_complete",
                     "warmup_count": self.attempt.policy["host"]["warmup_count"],
                     "sample_count": host_count, "samples_ns": [100] * host_count,
                     "statistics": {"median_ns": 100}, "repeatability_verified": True},
            "device": {"available": True, "valid": True, "instrumented": True,
                       "clock_mhz": 1000,
                       "capture": {"sample_count": device_count,
                                   "warmup": self.attempt.policy["device"]["warmup_count"]},
                       "artifacts": [artifact], "artifact_sha256": research.digest(csv_text.encode()),
                       "statistics": [{"zone": "kernel_main", "risc": "TRISC0",
                                       "count": device_count, "median_cycles": 200}],
                       "core_statistics": [{"device_id": 0, "core_x": 0, "core_y": 0,
                                            "program_index": 1, "count": device_count,
                                            "median_cycles": 200}]},
        }]
        self.attempt.finish({"status": 1, "score": 100}, "judge_completed", {
            "eligible_for_ranking": False, "reason_codes": [],
        })

    @staticmethod
    def overwrite(path, value):
        """A deliberate fixture corruption; the tempfile owner can chmod."""
        path.chmod(0o600)
        path.write_bytes(protocol.json_bytes(value) if isinstance(value, dict) else value)
        path.chmod(0o400)

    def inventory(self):
        return research.read_json(self.attempt.private / "inventory.json")

    def update_report_inventory(self, report):
        """Keep byte checks valid to exercise report/content consistency."""
        inventory = self.inventory()
        for namespace, base in (("private", self.attempt.private), ("public", self.attempt.public)):
            self.overwrite(base / "report.json", report)
            payload = (base / "report.json").read_bytes()
            item = next(item for item in inventory["files"][namespace] if item["path"] == "report.json")
            item.update({"sha256": research.digest(payload), "size": len(payload)})
        self.overwrite(self.attempt.private / "inventory.json", inventory)

    def test_generated_attempt_passes_audit(self):
        report, audit = research.verify_attempt(self.attempt.private, self.profiles)
        self.assertEqual(report["final"]["status_name"], "AC")
        self.assertEqual(audit["attempt_id"], self.attempt.id)
        self.assertGreater(audit["verified_artifacts"], 0)

    def test_omitted_source_inventory_is_rejected(self):
        inventory = self.inventory()
        inventory["files"]["private"] = [item for item in inventory["files"]["private"]
                                         if item["path"] != "submission.py"]
        self.overwrite(self.attempt.private / "inventory.json", inventory)
        with self.assertRaisesRegex(ValueError, "Inventory does not exactly cover"):
            research.verify_attempt(self.attempt.private, self.profiles)

    def test_source_deleted_with_inventory_entry_is_rejected(self):
        inventory = self.inventory()
        inventory["files"]["private"] = [item for item in inventory["files"]["private"]
                                         if item["path"] != "submission.py"]
        (self.attempt.private / "submission.py").unlink()
        self.overwrite(self.attempt.private / "inventory.json", inventory)
        with self.assertRaisesRegex(ValueError, "Source snapshot is absent"):
            research.verify_attempt(self.attempt.private, self.profiles)

    def test_corrupt_source_bytes_are_rejected(self):
        self.overwrite(self.attempt.private / "submission.py", b"changed source\n")
        with self.assertRaisesRegex(ValueError, "Artifact checksum mismatch: private/submission.py"):
            research.verify_attempt(self.attempt.private, self.profiles)

    def test_report_source_hash_mismatch_is_rejected(self):
        report = research.read_json(self.attempt.private / "report.json")
        report["environment"]["source_sha256"] = "0" * 64
        self.update_report_inventory(report)
        with self.assertRaisesRegex(ValueError, "Source fingerprint differs from archive"):
            research.verify_attempt(self.attempt.private, self.profiles)

    def test_device_warmup_mismatch_is_rejected(self):
        report = research.read_json(self.attempt.private / "report.json")
        report["cases"][0]["device"]["capture"]["warmup"] = 0
        self.update_report_inventory(report)
        with self.assertRaisesRegex(ValueError, "Device warmup count differs from protocol"):
            research.verify_attempt(self.attempt.private, self.profiles)

    def test_export_redacts_injected_text_and_preserves_numeric_metrics(self):
        report = research.read_json(self.attempt.private / "report.json")
        case = report["cases"][0]
        private_text = "PRIVATE_NOTE student@example.invalid"
        case["host"].update({"timing_scope": private_text, "reason": private_text})
        case["host"]["statistics"]["unrecognized_note"] = private_text
        case["device"]["reason"] = private_text
        case["device"]["statistics"][0].update({"zone": private_text, "risc": private_text,
                                                "unrecognized_note": private_text})
        case["device"]["core_statistics"][0]["unrecognized_note"] = private_text
        task = research.read_json(self.attempt.private / "task.json")
        row = research.export_row(report, task, b"test-private-key" * 3)
        serialized = json.dumps(row)
        for secret in (private_text, "PRIVATE_SOURCE", "PRIVATE_INPUT", "PRIVATE_ANSWER"):
            self.assertNotIn(secret, serialized)
        exported = row["cases"][0]
        self.assertEqual(exported["host"]["timing_scope"], "other_redacted")
        self.assertEqual(exported["host"]["reason"], "other_redacted")
        self.assertEqual(exported["device"]["reason"], "other_redacted")
        self.assertEqual(exported["host"]["statistics"]["median_ns"], 100)
        group = exported["device"]["statistics"][0]
        self.assertEqual(group["median_cycles"], 200)
        self.assertNotIn("zone", group)
        self.assertEqual(len(group["zone_pseudonym"]), 64)
        self.assertEqual(len(row["participant_pseudonym"]), 64)

    def test_wrapper_changes_split_aggregation_conditions(self):
        report = research.read_json(self.attempt.private / "report.json")
        same_report = copy.deepcopy(report)
        changed_report = copy.deepcopy(report)
        changed_report["environment"]["ttnn_wrapper_sha256"] = "wrapper-v2"
        changed_report["cases"][0]["host"]["statistics"]["median_ns"] = 1000
        entries = [{"implementation": "reference", "report": item}
                   for item in (report, same_report, changed_report)]
        summaries = experiment.aggregate(entries)
        for metric in ("host", "device_same_core_envelope"):
            groups = [item for item in summaries if item["metric"] == metric]
            self.assertEqual(len(groups), 2)
            self.assertEqual(sorted(item["independent_submission_count"] for item in groups), [1, 2])
            self.assertEqual({item["comparison_conditions"]["code_hashes"]["ttnn_wrapper_sha256"]
                              for item in groups}, {"wrapper-v1", "wrapper-v2"})


if __name__ == "__main__":
    unittest.main()
