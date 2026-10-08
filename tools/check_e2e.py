"""Exercise the real Hydro submission queue and physical TT worker.

Run from the project directory. --extended includes a full 120s timeout test.
Credentials are read from .env and never printed or written to the report.
"""
import argparse
import http.cookiejar
import json
from pathlib import Path
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
STATUS = {1: "AC", 2: "WA", 3: "TLE", 4: "MLE", 5: "OLE", 6: "RE", 7: "CE", 8: "SE"}


class Client:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, headers={"Content-Type": "application/json", "Accept": "application/json"})
        with self.opener.open(request, timeout=20) as response:
            result = json.load(response)
        if result.get("error"):
            raise RuntimeError(result["error"])
        return result

    def wait(self, rid, timeout=600):
        started = time.monotonic()
        previous = None
        while time.monotonic() - started < timeout:
            response = self.request(f"/record/{rid}")
            record = response["rdoc"]
            state = record["status"]
            if state != previous:
                print(f"  {rid}: status {STATUS.get(state, state)}", flush=True)
                previous = state
            if state in STATUS:
                return record
            time.sleep(1)
        raise TimeoutError(f"Record {rid} did not finish")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--extended", action="store_true")
    parser.add_argument("--base", default="http://127.0.0.1:8888")
    parser.add_argument("--only", nargs="+", help="Run only the named checks")
    parser.add_argument("--report", default="test-results.json")
    args = parser.parse_args()
    cfg = dict(line.split("=", 1) for line in (ROOT / ".env").read_text().splitlines() if "=" in line and not line.startswith("#"))
    client = Client(args.base)
    client.request("/login", {"uname": cfg["ADMIN_USERNAME"], "password": cfg["ADMIN_PASSWORD"]})
    checks = [
        ("TTNN addition", "TT1001", "ttnn", "ttnn-add/accepted.py", 1),
        ("TTNN matmul", "TT1002", "ttnn", "ttnn-matmul/accepted.py", 1),
        ("Metalium RISC-V", "TT2001", "metalium-riscv", "metalium-riscv/accepted.cpp", 1),
        ("Metalium compute", "TT2002", "metalium-compute", "metalium-compute/accepted.cpp", 1),
        ("TTNN wrong answer", "TT1001", "ttnn", "ttnn-add/wrong.py", 2),
        ("Metalium wrong answer", "TT2001", "metalium-riscv", "metalium-riscv/wrong.cpp", 2),
        ("Python syntax error", "TT1001", "ttnn", "ttnn-add/compile-error.py", 7),
        ("C++ compile error", "TT2001", "metalium-riscv", "metalium-riscv/compile-error.cpp", 7),
        ("Python runtime error", "TT1001", "ttnn", "ttnn-add/runtime-error.py", 6),
    ]
    if args.extended:
        checks += [
            ("Output limit", "TT1001", "ttnn", None, 5),
            ("Wall time limit", "TT1001", "ttnn", None, 3),
            ("Hardware after timeout", "TT1001", "ttnn", "ttnn-add/accepted.py", 1),
        ]
    if args.only:
        checks = [check for check in checks if check[0] in args.only]
        if len(checks) != len(args.only):
            raise ValueError("Unknown or duplicate check name in --only")
    report = []
    for name, pid, lang, source_path, expected in checks:
        if source_path:
            code = (ROOT / "problems" / source_path).read_text()
        elif expected == 5:
            code = "import sys\nsys.stdout.write('X' * (512 * 1024))\nsys.stdout.flush()\ndef solve(a,b): return a\n"
        else:
            code = "while True:\n    pass\ndef solve(a,b): return a\n"
        print(f"Submitting {name} to {pid}", flush=True)
        response = client.request(f"/p/{pid}/submit", {"lang": lang, "code": code})
        rid = response["rid"]
        record = client.wait(rid)
        entry = {"name": name, "problem": pid, "rid": rid, "expected": STATUS[expected], "actual": STATUS[record["status"]],
                 "score": record.get("score"), "time_ms": record.get("time"), "cases": len(record.get("testCases", []))}
        report.append(entry)
        (ROOT / args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"  Result: {entry['actual']} score={entry['score']} cases={entry['cases']}", flush=True)
        if record["status"] != expected:
            print(json.dumps({"judgeTexts": record.get("judgeTexts"), "compilerTexts": record.get("compilerTexts"),
                              "testCases": record.get("testCases")}, ensure_ascii=False, indent=2), flush=True)
            raise AssertionError(f"{name}: expected {STATUS[expected]}, got {entry['actual']}")
    print(f"All {len(report)} real OJ checks passed.", flush=True)


if __name__ == "__main__":
    main()
