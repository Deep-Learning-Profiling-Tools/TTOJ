"""Read-only HTTP checks for finished schema2 evaluation records.

This never submits code, rejudges records, or changes account preferences.
The existing administrator credentials are read from .env and never reported.
Run after test-results-protocol.json has been produced by check_e2e.py.
"""
import argparse
import hashlib
import http.cookiejar
import json
from pathlib import Path
import urllib.error
import urllib.request
from html.parser import HTMLParser


ROOT = Path(__file__).resolve().parents[1]


class RecordPage(HTMLParser):
    """Capture default-visible text and the native detail disclosure state."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.details = []
        self.detail_stack = []
        self.summary = False
        self.visible_text = []
        self.compiler_text = []
        self.judge_text = []
        self.pre_class = ""
        self.links = []
        self.device_summaries = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("script", "style"):
            self.skip += 1
        if tag == "details":
            item = {"class": attrs.get("class", ""), "open": "open" in attrs}
            self.details.append(item)
            self.detail_stack.append(item)
        elif tag == "summary":
            self.summary = True
        elif tag == "pre":
            self.pre_class = attrs.get("class", "")
        elif tag == "a":
            self.links.append(attrs.get("href", ""))
        elif tag == "table" and "tt-device-summary" in attrs.get("class", "").split():
            self.device_summaries += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip -= 1
        elif tag == "summary":
            self.summary = False
        elif tag == "details" and self.detail_stack:
            self.detail_stack.pop()
        elif tag == "pre":
            self.pre_class = ""

    def handle_data(self, data):
        if self.skip:
            return
        visible = all(item["open"] or (self.summary and i == len(self.detail_stack) - 1)
                      for i, item in enumerate(self.detail_stack))
        if visible:
            self.visible_text.append(data)
            if "compiler-text" in self.pre_class.split():
                self.compiler_text.append(data)
            if "judge-text" in self.pre_class.split():
                self.judge_text.append(data)


class Client:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def get(self, path, accept="text/html"):
        request = urllib.request.Request(self.base + path, headers={"Accept": accept})
        try:
            with self.opener.open(request, timeout=30) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    def login(self, username, password):
        request = urllib.request.Request(self.base + "/login",
            data=json.dumps({"uname": username, "password": password}).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json"})
        with self.opener.open(request, timeout=30) as response:
            result = json.load(response)
        if result.get("error"):
            raise RuntimeError("Existing administrator login failed; no credentials were changed.")


def validate(client, checks):
    results = []
    for check in checks:
        rid = check["rid"]
        status, _, raw_html = client.get(f"/record/{rid}?lang=en")
        assert status == 200, f"Record page unavailable: {rid}"
        page = RecordPage()
        page.feed(raw_html.decode())
        text = "".join(page.visible_text)
        status, headers, raw_report = client.get(f"/record/{rid}/profile", "application/json")
        assert status == 200, f"Profiling JSON unavailable: {rid}"
        assert "application/json" in headers.get("Content-Type", ""), "Wrong JSON MIME type"
        assert "attachment" in headers.get("Content-Disposition", ""), "Missing JSON download header"
        report = json.loads(raw_report)
        assert report["schema_version"] == 2, f"Expected schema2: {rid}"
        assert report["record_id"] == rid, "Downloaded record identity mismatch"
        saved = ROOT / "data/profiles" / rid / "report.json"
        assert raw_report == saved.read_bytes(), "Downloaded report differs from saved raw report"
        entry = {"record_id": rid, "problem": check["problem"], "verdict": check["actual"],
                 "schema_version": 2, "json_download_matches_raw": True}

        if check["actual"] == "AC":
            assert report["cases"], "AC report has no testcase measurements"
            assert all(not item["open"] for item in page.details), "AC technical details default open"
            host_count = 0
            device_count = 0
            csv_count = 0
            for case in report["cases"]:
                host, device = case["host"], case["device"]
                if host.get("available"):
                    host_count += 1
                    assert any(label in text for label in (
                        "Host submission to device completion", "主机提交至设备完成")), "Host summary hidden"
                    assert f"{host['statistics']['median_ns'] / 1000:.3f}" in text, "Host median absent"
                expected_samples = device.get("capture", {}).get("sample_count", 0)
                groups = [group for group in device.get("core_statistics", [])
                          if expected_samples > 0 and group.get("count") == expected_samples
                          and isinstance(group.get("median_ns"), (int, float))]
                if device.get("valid") and groups:
                    device_count += 1
                    largest = max(groups, key=lambda group: group["median_ns"])
                    assert any(label in text for label in (
                        "Device timing — slowest core median", "设备计时 · 最慢核心中位数")), "Device summary hidden"
                    assert f"{largest['median_ns'] / 1000:.3f}" in text, "Device median absent"
                for artifact in device.get("artifacts", []):
                    path = f"/record/{rid}/profile/{artifact}"
                    assert path in page.links, "CSV download link absent"
                    status, csv_headers, raw_csv = client.get(path, "text/csv")
                    assert status == 200, "CSV download failed"
                    assert "text/csv" in csv_headers.get("Content-Type", ""), "Wrong CSV MIME type"
                    assert raw_csv == (saved.parent / artifact).read_bytes(), "CSV download differs from saved raw trace"
                    assert hashlib.sha256(raw_csv).hexdigest() == device["artifact_sha256"], "CSV digest mismatch"
                    csv_count += 1
            assert host_count > 0, "AC record lacks a host summary"
            assert device_count > 0 and csv_count > 0, "AC record lacks device summaries or CSV traces"
            assert page.device_summaries == device_count, "Device summary count differs from raw report"
            entry.update({"default_details_closed": len(page.details), "host_summaries": host_count,
                          "device_summaries": device_count, "csv_downloads_match_raw": csv_count})
        elif check["actual"] == "CE":
            assert "".join(page.compiler_text).strip(), "Compiler diagnostics are not visible"
            if any(item["class"] == "tt-judge-log" for item in page.details):
                assert any(item["class"] == "tt-judge-log" and item["open"] for item in page.details), "CE judge log is closed"
            entry["compiler_diagnostics_visible"] = True
        elif check["actual"] == "RE":
            assert any(item["class"] == "tt-judge-log" and item["open"] for item in page.details), "RE judge log is closed"
            assert "".join(page.judge_text).strip(), "Runtime diagnostics are not visible"
            entry["runtime_diagnostics_visible"] = True

        denied = ["manifest.json", "events.jsonl", "task.json", "policy.json",
                  "submission.py", "solution.cpp", "source.py", "source.cpp", "latest.json"]
        attempt_id = report["attempt_id"]
        paths = [f"/record/{rid}/profile/{name}" for name in denied]
        paths.extend([f"/record/{rid}/profile/attempts/{attempt_id}/report.json",
                      f"/record/{rid}/profile/attempts%2F{attempt_id}%2Freport.json",
                      f"/record/{rid}/profile/%2e%2e%2Fattempts%2F{attempt_id}%2Freport.json"])
        for path in paths:
            status, _, _ = client.get(path, "application/json")
            assert status == 404, f"Non-public profile artifact unexpectedly reachable: {path}"
        entry["private_and_attempt_routes_rejected"] = len(paths)
        results.append(entry)
    assert any(entry["verdict"] == "AC" for entry in results), "Input lacks AC checks"
    assert any(entry["verdict"] == "CE" for entry in results), "Input lacks CE checks"
    assert any(entry["verdict"] == "RE" for entry in results), "Input lacks RE checks"
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8888")
    parser.add_argument("--input", default="test-results-protocol.json")
    parser.add_argument("--report", default="protocol-ui-validation.json")
    args = parser.parse_args()
    checks = json.loads((ROOT / args.input).read_text())
    cfg = dict(line.split("=", 1) for line in (ROOT / ".env").read_text().splitlines()
               if "=" in line and not line.startswith("#"))
    client = Client(args.base)
    client.login(cfg["ADMIN_USERNAME"], cfg["ADMIN_PASSWORD"])
    results = validate(client, checks)
    (ROOT / args.report).write_text(json.dumps(results, indent=2))
    print(json.dumps({"records_verified": len(results), "report": args.report}, indent=2))


if __name__ == "__main__":
    main()
