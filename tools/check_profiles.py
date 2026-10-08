"""Validate saved profiles from the four real OJ submissions, without devices."""
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main():
    checks = json.loads((ROOT / "test-results-dual-profile.json").read_text())
    assert len(checks) == 4 and all(check["actual"] == "AC" for check in checks)
    results = []
    for check in checks:
        folder = ROOT / "data/profiles" / check["rid"]
        report = json.loads((folder / "report.json").read_text())
        assert len(report["cases"]) == check["cases"]
        assert report["scheduler"]["concurrency"] == 1
        assert report["quality"]["eligible_for_ranking"] is False
        for case in report["cases"]:
            host, device = case["host"], case["device"]
            assert case["status"] == 1
            assert host["available"] and host["warmup_count"] == 5
            assert host["sample_count"] == len(host["samples_ns"]) == 50
            assert host["timing_scope"] == "host_submit_to_device_complete"
            assert device["available"] and device["valid"] and not device["errors"]
            assert device["capture"]["warmup"] == 5 and device["capture"]["sample_count"] == 10
            assert {event["sample"] for event in device["kernels"]} == set(range(1, 11))
            assert all(stage["count"] == 10 and stage["complete"] for stage in device["statistics"])
            raw = (folder / device["artifacts"][0]).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == device["artifact_sha256"]
            assert device["capture"]["sample_byte_ranges"][-1]["end_byte"] <= len(raw)
            assert device["raw_kernel_count"] > len(device["kernels"])
        results.append({"problem": check["problem"], "record_id": check["rid"],
                        "cases": len(report["cases"]), "host_samples_per_case": 50,
                        "device_samples_per_case": 10, "artifact_integrity": "pass"})
    (ROOT / "profile-validation.json").write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
