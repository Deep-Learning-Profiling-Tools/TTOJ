"""Hydro judge adapter with fixed, isolated Tenstorrent execution recipes.

The broker has Docker access. Submitted programs never receive the socket,
credentials, expected outputs, host home directory, or a network connection.
The hardware is reserved serially: the TT runtime discovers the entire mesh.
"""
import asyncio
import contextlib
from datetime import datetime, timezone
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import re
import signal
import socket
import threading
import time
import uuid

import aiohttp
import docker
from docker.types import LogConfig, Ulimit
from yarl import URL
from metrics import quality_report, sha256, summarize_profile, telemetry_snapshot
from device_profile import parse_device_profile
from protocol import Attempt, load_policy, recover_interrupted

AC, WA, TLE, MLE, OLE, RE, CE, SE = range(1, 9)
LANGS = ("ttnn", "metalium-riscv", "metalium-compute")
MARKER = "TT_OJ_RESULT:"
PROFILE_MARKER = "TT_OJ_PROFILE:"
OUTPUT_LIMIT = 256 * 1024
BASE_URL = os.environ.get("HYDRO_URL", "http://site:8888/").rstrip("/") + "/"
JOBS = Path(os.environ.get("JOBS_DIR", "/jobs"))
HOST_JOBS = Path(os.environ["JOBS_HOST_DIR"])
TTNN_IMAGE = os.environ["TTNN_IMAGE"]
METALIUM_IMAGE = os.environ.get("METALIUM_IMAGE", "tt-oj-metalium:0.1")
log = logging.getLogger("tt-judge")
PROFILES = Path(os.environ.get("PROFILES_DIR", "/profiles"))
BENCHMARK_CPUSET = os.environ.get("BENCHMARK_CPUSET", "4-7,12-15")
DEVICE_PROFILER_SUPPORTED = os.environ.get("TT_OJ_DEVICE_PROFILER_SUPPORTED") == "1"
DEVICE_CSV_PATH = "/tmp/generated/profiler/.logs/profile_log_device.csv"
DEVICE_ARTIFACT_LIMIT = 16 * 1024 * 1024


def compare_numbers(actual, expected, atol=0, rtol=0):
    """Require matching shape and finite numeric values at every element."""
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(
            compare_numbers(a, b, atol, rtol) for a, b in zip(actual, expected)
        )
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        return isinstance(actual, (int, float)) and not isinstance(actual, bool) and math.isfinite(actual) and math.isclose(
            actual, expected, abs_tol=atol, rel_tol=rtol
        )
    return actual == expected


class Execution:
    """A container lifecycle that can be stopped on timeout/disconnect/shutdown."""
    def __init__(self, client, container, profiling=False, attempt=None, case_id=None):
        self.client = client
        self.container = container
        self.out = bytearray()
        self.err = bytearray()
        self.output_exceeded = False
        self.peak_memory = 0
        self.thread_error = None
        self.telemetry = []
        self.profiling = profiling
        self.attempt = attempt
        self.case_id = case_id
        self.pass_name = "device" if profiling else "host"
        self.reader = threading.Thread(target=self._read, daemon=True)

    def _read(self):
        try:
            for stdout, stderr in self.container.attach(stream=True, logs=True, demux=True):
                for data, target in ((stdout, self.out), (stderr, self.err)):
                    if data:
                        remaining = OUTPUT_LIMIT - len(self.out) - len(self.err)
                        target.extend(data[:max(0, remaining)])
                        if len(data) > remaining:
                            self.output_exceeded = True
        except Exception as error:
            self.thread_error = str(error)

    def stop(self):
        if self.attempt:
            try:
                self.attempt.event("container_stop_requested", case_id=self.case_id, pass_name=self.pass_name,
                                   container_id=self.container.id)
            except Exception:
                log.exception("Could not journal stop request; terminating container regardless")
        with contextlib.suppress(docker.errors.NotFound, docker.errors.APIError):
            self.container.kill()

    def remove(self):
        with contextlib.suppress(docker.errors.NotFound, docker.errors.APIError):
            self.container.remove(force=True)

    def collect_profiler(self):
        """Read inside the live mount namespace; Docker archive misses tmpfs."""
        request = self.client.api.exec_create(self.container.id,
            ["/usr/bin/head", "-c", str(DEVICE_ARTIFACT_LIMIT + 1), DEVICE_CSV_PATH],
            stdout=True, stderr=True, user="1005:1005")
        payload, errors = bytearray(), bytearray()
        for stdout, stderr in self.client.api.exec_start(request["Id"], stream=True, demux=True):
            if stdout:
                payload.extend(stdout)
            if stderr:
                errors.extend(stderr[:1000])
        code = self.client.api.exec_inspect(request["Id"])["ExitCode"]
        if code != 0 or errors or len(payload) > DEVICE_ARTIFACT_LIMIT:
            return None
        return payload.decode("utf-8", "replace")

    def acknowledge_profiler(self):
        # Docker put_archive rejects a readonly root even when /tmp is writable.
        # Execute a fixed acknowledgement in tmpfs as the sandbox's own UID.
        result = self.container.exec_run(["/usr/bin/touch", "/tmp/tt-oj-artifact-ack"], user="1005:1005")
        if result.exit_code != 0:
            raise RuntimeError("Failed to acknowledge profiler artifact collection")

    def run(self, timeout):
        start = time.monotonic()
        if self.attempt:
            self.attempt.event("container_start_requested", case_id=self.case_id, pass_name=self.pass_name,
                               container_id=self.container.id, timeout_s=timeout)
        self.container.start()
        if self.attempt:
            self.attempt.event("container_started_observed", case_id=self.case_id, pass_name=self.pass_name,
                               container_id=self.container.id)
        self.reader.start()
        status = None
        profiler_csv = None
        artifact_collected = False
        while True:
            elapsed = time.monotonic() - start
            if self.output_exceeded:
                status = OLE
                self.stop()
                break
            if elapsed > timeout:
                status = TLE
                self.stop()
                break
            self.container.reload()
            if not self.container.attrs["State"]["Running"]:
                break
            if self.profiling and not artifact_collected:
                completed = re.search(rb"(?m)^TT_OJ_DEVICE_CAPTURE_DONE:([0-9]+)\r?$", bytes(self.out))
                if completed is None:
                    # Very short child programs can finish before attach starts.
                    completed = re.search(rb"(?m)^TT_OJ_DEVICE_CAPTURE_DONE:([0-9]+)\r?$",
                        self.container.logs(stdout=True, stderr=False, tail=2))
                if completed:
                    try:
                        if int(completed.group(1)) == 0:
                            profiler_csv = self.collect_profiler()
                    finally:
                        self.acknowledge_profiler()
                    artifact_collected = True
            try:
                memory = self.container.stats(stream=False, one_shot=True).get("memory_stats", {}).get("usage", 0)
                self.peak_memory = max(self.peak_memory, memory)
                self.telemetry.append(telemetry_snapshot())
            except docker.errors.APIError:
                pass
            time.sleep(0.15)
        self.reader.join(timeout=3)
        self.container.reload()
        state = self.container.attrs["State"]
        if state.get("OOMKilled"):
            status = MLE
        # Recover any output emitted before the streaming attachment connected.
        stdout = self.container.logs(stdout=True, stderr=False)
        stderr = self.container.logs(stdout=False, stderr=True)
        if len(stdout) + len(stderr) > OUTPUT_LIMIT:
            self.output_exceeded = True
        self.out = bytearray(stdout[:OUTPUT_LIMIT])
        self.err = bytearray(stderr[:max(0, OUTPUT_LIMIT - len(self.out))])
        if status is None and state.get("ExitCode", 1):
            status = RE
        if status is None and self.output_exceeded:
            status = OLE
        if status is None and self.thread_error:
            raise RuntimeError(f"Docker output stream failed: {self.thread_error}")
        wall_ms = round((time.monotonic() - start) * 1000, 2)
        if self.attempt:
            self.attempt.event("container_exit_observed", case_id=self.case_id, pass_name=self.pass_name,
                               container_id=self.container.id, exit_code=state.get("ExitCode"), status=status,
                               wall_time_ms=wall_ms, output_truncated=self.output_exceeded)
        return {
            "status": status,
            "time": wall_ms,
            "memory": math.ceil(self.peak_memory / 1024),
            "stdout": self.out.decode("utf-8", "replace"),
            "stderr": self.err.decode("utf-8", "replace"),
            "exit_code": state.get("ExitCode"),
            "telemetry": self.telemetry,
            "profiler_csv": profiler_csv,
        }


class Judge:
    def __init__(self):
        self.docker = docker.from_env()
        self.http = None
        self.ws = None
        self.active = None
        self.judging = None
        self.shutting_down = False
        self.attempt = None
        _, self.policy = load_policy()
        JOBS.mkdir(parents=True, exist_ok=True)
        PROFILES.mkdir(parents=True, exist_ok=True)
        self.lock_file = open(JOBS / "hardware.lock", "a")
        fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Only recover containers created by this worker; never touch other jobs.
        for container in self.docker.containers.list(all=True, filters={"label": "tt-oj.worker=local"}):
            container.remove(force=True)
        recovered = recover_interrupted(JOBS, PROFILES)
        if recovered:
            log.warning("Finalized %s interrupted attempt archives after restart", recovered)

    async def post(self, path, payload):
        async with self.http.post(BASE_URL + path.lstrip("/"), json=payload) as response:
            if response.status >= 400:
                raise RuntimeError(f"Hydro {path} returned HTTP {response.status}: {(await response.text())[:800]}")
            result = await response.json()
            if isinstance(result, dict) and result.get("error"):
                raise RuntimeError(f"Hydro {path}: {result['error']}")
            return result

    async def login(self):
        await self.post("login", {
            "uname": os.environ["JUDGE_USERNAME"],
            "password": os.environ["JUDGE_PASSWORD"],
            "rememberme": "on", "judge": "on",
        })
        cookies = self.http.cookie_jar.filter_cookies(URL(BASE_URL))
        if "sid" not in cookies:
            raise RuntimeError("Hydro login did not return a session")
        return cookies["sid"].value

    async def manifest(self, task):
        domain_id = task["domainId"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", domain_id):
            raise ValueError("Invalid domain id")
        response = await self.post(f"d/{domain_id}/judge/files", {"pid": int(task["pid"]), "files": ["manifest.json"]})
        link = response["links"]["manifest.json"]
        async with self.http.get(BASE_URL + link.lstrip("/") if link.startswith("/") else link) as resp:
            resp.raise_for_status()
            raw = await resp.content.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError("Problem manifest is too large")
        return json.loads(raw), raw

    async def send(self, rid, key, **payload):
        await self.ws.send_json({"rid": rid, "key": key, **payload})

    def prepare(self, folder, task, manifest, case, profiling=False):
        folder.mkdir(parents=True, exist_ok=True)
        mode = manifest["mode"]
        source_name = "submission.py" if mode == "ttnn" else "solution.cpp"
        (folder / source_name).write_text(task["code"], encoding="utf-8")
        (folder / "input.txt").write_text(case["input"], encoding="utf-8")
        if mode == "ttnn":
            (folder / "runner.py").write_bytes(Path("/app/ttnn_runner.py").read_bytes())
        host_folder = HOST_JOBS / folder.relative_to(JOBS)
        # Pass all mesh device nodes required by topology discovery. Serial lock
        # protects the complete mesh; programs use device 0 in the initial tasks.
        devices = [f"/dev/tenstorrent/{i}:/dev/tenstorrent/{i}:rwm" for i in range(4)]
        executable = "/opt/venv/bin/python3 /workspace/runner.py" if mode == "ttnn" else (
            "/opt/tt-oj/bin/judge-riscv" if mode == "metalium-riscv" else "/opt/tt-oj/bin/judge-compute"
        )
        # Resolve a mutable tag once per attempt; every pass executes that ID.
        image = task.get("_runtime_image_id") or (TTNN_IMAGE if mode == "ttnn" else METALIUM_IMAGE)
        environment = {
            "HOME": "/tmp", "PYTHONUNBUFFERED": "1", "TT_METAL_LOGGER_LEVEL": "warning",
            "TT_OJ_BENCHMARK": "0" if profiling else "1",
            "TT_OJ_WARMUP": str(self.policy["host"]["warmup_count"]),
            "TT_OJ_SAMPLES": str(self.policy["host"]["sample_count"]),
            "TT_OJ_PROFILE_ONLY": "1" if profiling else "0",
            "TT_OJ_DEVICE_PROFILER_SUPPORTED": "1" if DEVICE_PROFILER_SUPPORTED else "0",
            "TT_METAL_PROFILER_DIR": "/tmp/generated/profiler",
            "TT_METAL_RUNTIME_ROOT": "/opt/tt-profiler/libexec/tt-metalium",
        }
        if profiling:
            environment["TT_METAL_DEVICE_PROFILER"] = "1"
            environment["TT_METAL_PROFILER_DISABLE_PUSH_TO_TRACY"] = "1"
            environment["TT_METAL_PROFILER_CPP_POST_PROCESS"] = "0"
            environment["TT_METAL_PROFILER_MID_RUN_DUMP"] = "1"
        command = f"exec {executable} < /workspace/input.txt"
        if profiling:
            command = (f"{executable} < /workspace/input.txt; tt_oj_exit=$?; "
                "printf '\\nTT_OJ_DEVICE_CAPTURE_DONE:%s\\n' \"$tt_oj_exit\"; "
                "while [ ! -f /tmp/tt-oj-artifact-ack ]; do sleep 0.05; done; exit \"$tt_oj_exit\"")
        container = self.docker.containers.create(
            image, ["-c", command], entrypoint="/bin/sh",
            name=f"tt-oj-job-{uuid.uuid4().hex[:16]}",
            user="1005:1005", working_dir="/tmp", environment=environment,
            volumes={str(host_folder): {"bind": "/workspace", "mode": "ro"}},
            devices=devices, network_mode="none", read_only=True,
            cap_drop=["ALL"], security_opt=["no-new-privileges:true"],
            mem_limit="8g", memswap_limit="8g", cpuset_cpus=BENCHMARK_CPUSET,
            pids_limit=512, shm_size="128m",
            tmpfs={"/tmp": "rw,exec,size=2147483648,mode=1777"},
            ulimits=[Ulimit(name="nofile", soft=4096, hard=4096)],
            log_config=LogConfig(type="json-file", config={"max-size": "1m", "max-file": "1"}),
            labels={"tt-oj.worker": "local", "tt-oj.record": task["rid"],
                    "tt-oj.attempt": self.attempt.id if self.attempt else "unknown"},
        )
        case_id = int(folder.name.split("-")[0])
        if self.attempt:
            try:
                self.attempt.event("container_created", case_id=case_id,
                                   pass_name="device" if profiling else "host", container_id=container.id,
                                   runtime_image_id=image, cpu_affinity=BENCHMARK_CPUSET)
            except Exception:
                with contextlib.suppress(docker.errors.NotFound, docker.errors.APIError):
                    container.remove(force=True)
                raise
        return Execution(self.docker, container, profiling=profiling, attempt=self.attempt, case_id=case_id)

    async def wait_for_hardware(self, rid, timeout=120):
        """Exclude known external Docker TT workloads and excessive heat.

        The flock is an OJ coordination lock. This check adds Docker workload
        detection, not a claim of excluding arbitrary host processes.
        """
        start = time.monotonic()
        while True:
            foreign = []
            for container in await asyncio.to_thread(self.docker.containers.list):
                if container.labels.get("tt-oj.worker") == "local":
                    continue
                configs = container.attrs.get("HostConfig", {})
                devices = configs.get("Devices") or []
                mounts = container.attrs.get("Mounts") or []
                if any(str(d.get("PathOnHost", "")).startswith("/dev/tenstorrent") for d in devices) or any(
                    str(m.get("Source", "")).startswith("/dev/tenstorrent") for m in mounts
                ):
                    foreign.append(container.name)
            telemetry = telemetry_snapshot()
            hot = any(t["value"] >= 85 for t in telemetry.get("temperatures_c", []))
            if not foreign and not hot:
                return {"known_external_containers": [], "thermal_preflight": "pass", "wait_ms": (time.monotonic() - start) * 1000}
            if time.monotonic() - start > timeout:
                raise RuntimeError(f"Hardware reservation unavailable: external TT containers={foreign}, thermal_limit={hot}")
            await self.send(rid, "next", message="等待硬件空闲或降温 / Waiting for idle hardware or cooldown.")
            await asyncio.sleep(2)

    async def prepare_execution(self, folder, task, manifest, case, profiling=False):
        # Cancellation must not abandon a still-running Docker create thread.
        args = (folder, task, manifest, case, profiling) if profiling else (folder, task, manifest, case)
        preparing = asyncio.create_task(asyncio.to_thread(self.prepare, *args))
        try:
            return await asyncio.shield(preparing)
        except asyncio.CancelledError:
            try:
                execution = await preparing
                await asyncio.to_thread(execution.remove)
            except Exception:
                log.exception("Failed to clean up cancelled container preparation")
            raise

    async def run_execution(self, execution, timeout):
        running = asyncio.create_task(asyncio.to_thread(execution.run, timeout))
        outcome = None
        try:
            outcome = await asyncio.shield(running)
            return outcome
        except asyncio.CancelledError:
            await asyncio.to_thread(execution.stop)
            # A cancellation can race container.start(). Force-remove even a
            # created container before waiting for the execution thread.
            await asyncio.to_thread(execution.remove)
            with contextlib.suppress(Exception):
                await running
            raise
        finally:
            try:
                if execution.attempt:
                    execution.attempt.capture(execution.case_id, execution.pass_name, execution, outcome)
            finally:
                await asyncio.to_thread(execution.remove)

    async def judge(self, task):
        rid = str(task.get("rid", ""))
        if not re.fullmatch(r"[0-9a-fA-F]{24}", rid):
            log.error("Rejected task with invalid record id")
            return
        task = dict(task)
        task["rid"] = rid = rid.lower()
        attempt = None
        log.info("Judging record=%s language=%s problem=%s", rid, task.get("lang"), task.get("pid"))

        async def end(status, reason, **payload):
            final = {"status": status, "score": 0, **payload}
            # Full diagnostics can contain code snippets and hidden input. They
            # remain in the private archive and the normal owner-only judge log.
            attempt.write_private("terminal-details.json", (json.dumps(final, ensure_ascii=False, indent=2) + "\n").encode())
            safe_final = {key: value for key, value in final.items() if key in ("status", "score", "time", "memory")}
            safe_final["message"] = reason
            attempt.report["environment"]["after"] = telemetry_snapshot()
            quality = quality_report(attempt.report["cases"])
            if status != AC:
                quality["reason_codes"] = list(dict.fromkeys(quality["reason_codes"] + ["attempt_not_accepted"]))
            attempt.finish(safe_final, reason, quality)
            await self.send(rid, "end", **final)

        try:
            attempt = self.attempt = Attempt(JOBS, PROFILES, task)
            report = attempt.report
            report["scheduler"] = {"concurrency": 1, "reservation": "whole_mesh", "device_ids": [0],
                "discovered_devices": [0, 1, 2, 3], "cpu_affinity": BENCHMARK_CPUSET,
                "cpu_quota": None, "queue_priority": task.get("priority"),
                "queue_policy": "Hydro priority queue", "external_contention_verified": False}
            code_paths = [Path(__file__), Path(__file__).with_name("metrics.py"), Path(__file__).with_name("device_profile.py"),
                          Path(__file__).with_name("protocol.py"), Path('/app/ttnn_runner.py')]
            code_paths.extend(sorted(Path('/app/reference').glob('*')))
            report["environment"].update({
                "ttnn_wrapper_sha256": sha256(Path('/app/ttnn_runner.py').read_bytes()),
                "worker_code_sha256": {path.name: sha256(path.read_bytes()) for path in code_paths if path.is_file()},
                "sdk_commit": "de546d3b146758714d900f11b218c8f9c805f410",
                "sdk_build": "v0.79.0 ReleaseTracy; profiler enabled only in separate diagnostic pass",
                "runtime_images": {"ttnn": TTNN_IMAGE, "metalium": METALIUM_IMAGE},
                "before": telemetry_snapshot(), "host_kernel": os.uname().release,
                "worker_hostname": socket.gethostname(), "image_ids": {},
            })
            report["environment"].update({name: sha256(path.read_bytes()) for name, path in (
                ("worker_sha256", Path(__file__)), ("metrics_sha256", Path(__file__).with_name("metrics.py")),
                ("device_parser_sha256", Path(__file__).with_name("device_profile.py")),
                ("protocol_sha256", Path(__file__).with_name("protocol.py")))})
            attempt.checkpoint()
            await self.send(rid, "next", status=21, message="Preparing Tenstorrent containers; whole-machine serial evaluation.")
            attempt.event("manifest_fetch_started")
            manifest, manifest_raw = await self.manifest(task)
            attempt.set_manifest(manifest_raw, manifest)
            if not report["correctness_policy"]["tolerances_valid"]:
                raise ValueError("Manifest atol and rtol must be finite nonnegative numbers")
            mode = manifest["mode"]
            if mode not in LANGS or task.get("lang") != manifest["language"]:
                raise ValueError("Submission language does not match this problem's execution mode")
            if not isinstance(task.get("code"), str) or not task["code"] or task.get("files", {}).get("code"):
                await end(CE, "text_source_required", compilerText="Submit text source; binary or ZIP projects are not supported.")
                return
            if len(task["code"].encode()) > 256 * 1024:
                await end(CE, "source_exceeds_256_kib", compilerText="Source exceeds 256 KiB.")
                return
            if mode == "ttnn":
                attempt.event("syntax_check_started")
                try:
                    compile(task["code"], "submission.py", "exec")
                except SyntaxError as error:
                    await end(CE, "python_syntax_error", compilerText=str(error))
                    return
                attempt.event("syntax_check_passed")
            images = {name: self.docker.images.get(image) for name, image in (("ttnn", TTNN_IMAGE), ("metalium", METALIUM_IMAGE))}
            report["environment"]["image_ids"] = {name: image.id for name, image in images.items()}
            report["environment"]["image_repo_digests"] = {name: image.attrs.get("RepoDigests", []) for name, image in images.items()}
            task["_runtime_image_id"] = images["ttnn" if mode == "ttnn" else "metalium"].id
            report["environment"]["executed_runtime_image_id"] = task["_runtime_image_id"]
            try:
                report["environment"]["worker_image_id"] = self.docker.containers.get(socket.gethostname()).image.id
            except docker.errors.DockerException:
                report["environment"]["worker_image_id"] = None
            attempt.event("runtime_images_resolved", image_ids=report["environment"]["image_ids"])
            attempt.checkpoint()
            attempt.event("hardware_preflight_started")
            report["scheduler"]["preflight"] = await self.wait_for_hardware(rid)
            attempt.event("hardware_preflight_passed", **report["scheduler"]["preflight"])
            cases = manifest["tests"]
            time_ms = manifest.get("time_ms", 120000)
            if isinstance(time_ms, bool) or not isinstance(time_ms, (int, float)) or not math.isfinite(time_ms):
                raise ValueError("Manifest time_ms must be finite")
            timeout = min(max(time_ms / 1000, 1), 180)
            if task.get("input"):
                inputs = task["input"] if isinstance(task["input"], list) else [task["input"]]
                cases = [{"input": text, "output": None} for text in inputs]
            if not cases or len(cases) > 30:
                raise ValueError("Problem must have 1 to 30 cases")
            report["expected_case_count"] = len(cases)
            report["execution_limits"] = {"per_pass_wall_timeout_s": timeout, "host_memory_bytes": 8 * 1024 ** 3,
                "output_capture_bytes": OUTPUT_LIMIT, "device_csv_bytes": DEVICE_ARTIFACT_LIMIT}
            attempt.checkpoint()
            results, final_status, score = [], AC, 0
            for index, case in enumerate(cases, 1):
                case_started = time.monotonic_ns()
                profile_case = {"id": index, "state": "running", "status": None,
                    "correctness": {"reference_available": case["output"] is not None,
                        "input_sha256": sha256(case["input"].encode()),
                        "expected_output_sha256": sha256(case["output"].encode()) if case["output"] is not None else None,
                        "atol": manifest.get("atol", 0), "rtol": manifest.get("rtol", 0),
                        "host_comparison": "not_run", "device_comparison": "not_run",
                        "repeat_check_scope": "first_and_final_not_every_invocation"},
                    "host": {"available": False, "reason": "not_run"},
                    "device": {"available": False, "reason": "not_run"},
                    "started_at": datetime.now(timezone.utc).isoformat(), "started_monotonic_ns": case_started}
                report["cases"].append(profile_case)
                attempt.event("case_started", case_id=index)
                attempt.checkpoint()
                await self.send(rid, "next", status=20, progress=round((index - 1) * 100 / len(cases)))
                folder = attempt.private / str(index)
                attempt.event("pass_prepare_started", case_id=index, pass_name="host")
                self.active = await self.prepare_execution(folder, task, manifest, case)
                try:
                    outcome = await self.run_execution(self.active, timeout)
                finally:
                    self.active = None
                status, message = outcome["status"], ""
                full_log = outcome["stderr"] + "\n" + outcome["stdout"]
                if status == RE and mode != "ttnn" and re.search(r"(?:error:|Build failed|compilation failed|JIT compilation|CompileError)", full_log, re.I):
                    status = CE
                    await self.send(rid, "next", compilerText=full_log[-10000:])
                if status is None:
                    markers = [line[len(MARKER):] for line in outcome["stdout"].splitlines() if line.startswith(MARKER)]
                    if len(markers) != 1:
                        status, message = RE, "Program did not produce a unique result; check kernel completion."
                        profile_case["correctness"]["host_comparison"] = "missing_or_duplicate_result_marker"
                    else:
                        try:
                            actual = json.loads(markers[0])
                            profile_case["correctness"]["actual_output_sha256"] = sha256(markers[0].encode())
                            if case["output"] is None:
                                status, message = AC, "Self-test output: " + markers[0][:2000]
                                profile_case["correctness"]["host_comparison"] = "self_test_no_reference"
                            else:
                                matches = compare_numbers(actual, json.loads(case["output"]), manifest.get("atol", 0), manifest.get("rtol", 0))
                                status = AC if matches else WA
                                message = "All elements passed validation." if matches else "Output values or tensor shape differ from the reference."
                                profile_case["correctness"]["host_comparison"] = "pass" if matches else "fail"
                        except (ValueError, TypeError) as error:
                            status, message = RE, f"Invalid result format: {error}"
                            profile_case["correctness"]["host_comparison"] = "invalid_result_format"
                if status in (RE, CE) and not message:
                    message = full_log[-4000:]
                if status == TLE:
                    message = f"Exceeded the {timeout:g}s per-case wall limit including initialization and JIT; container terminated."
                if status == MLE:
                    message = "Exceeded the 8 GiB host memory limit."
                if status == OLE:
                    message = "stdout/stderr exceeded 256 KiB."
                result = {"id": index, "subtaskId": 1, "status": status, "score": 100 / len(cases) if status == AC else 0,
                          "time": outcome["time"], "memory": outcome["memory"], "message": message}
                host_profile = {"available": False, "reason": "no_profile_produced"}
                profile_lines = [line[len(PROFILE_MARKER):] for line in outcome["stdout"].splitlines() if line.startswith(PROFILE_MARKER)]
                if len(profile_lines) == 1:
                    try:
                        host_profile = summarize_profile(json.loads(profile_lines[0]))
                    except (ValueError, TypeError):
                        host_profile = {"available": False, "reason": "invalid_profile_json"}
                profile_case.update({"status": status, "wall_time_ms": outcome["time"], "host": host_profile,
                                     "telemetry": outcome.get("telemetry", []), "runtime_affinity_warning": "Unable to bind" in full_log})
                repeat_verified = host_profile.get("repeatability_verified", host_profile.get("correctness_verified"))
                profile_case["correctness"]["host_repeatability_verified"] = repeat_verified
                attempt.event("pass_completed", case_id=index, pass_name="host", status=status,
                              comparison=profile_case["correctness"]["host_comparison"], repeatability_verified=repeat_verified)
                attempt.checkpoint()
                device_profile = {"available": False, "reason": "skipped_non_accepted_case" if status != AC else "profiler_enabled_sdk_required"}
                if status == AC and DEVICE_PROFILER_SUPPORTED:
                    await self.send(rid, "next", message=f"Case {index}: collecting a separate device profiler pass.")
                    attempt.event("pass_prepare_started", case_id=index, pass_name="device")
                    self.active = await self.prepare_execution(attempt.private / f"{index}-device", task, manifest, case, profiling=True)
                    try:
                        diagnostic = await self.run_execution(self.active, timeout)
                    finally:
                        self.active = None
                    if diagnostic.get("profiler_csv"):
                        # Preserve a collected CSV even when the diagnostic
                        # program later fails; never reinterpret it as valid.
                        filename = attempt.write_csv(index, diagnostic["profiler_csv"])
                    else:
                        filename = None
                    if diagnostic["status"] is None and filename:
                        capture_lines = [line[len(PROFILE_MARKER):] for line in diagnostic["stdout"].splitlines() if line.startswith(PROFILE_MARKER)]
                        diagnostic_results = [line[len(MARKER):] for line in diagnostic["stdout"].splitlines() if line.startswith(MARKER)]
                        try:
                            metadata = json.loads(capture_lines[0]) if len(capture_lines) == 1 else {}
                            capture = metadata.get("device_capture")
                            diagnostic_correct = len(diagnostic_results) == 1 and (case["output"] is None or compare_numbers(
                                json.loads(diagnostic_results[0]), json.loads(case["output"]), manifest.get("atol", 0), manifest.get("rtol", 0)))
                            profile_case["correctness"]["device_comparison"] = ("self_test_no_reference" if case["output"] is None else "pass") if diagnostic_correct else "fail"
                            profile_case["correctness"]["device_repeatability_verified"] = metadata.get("repeatability_verified", metadata.get("correctness_verified"))
                            if not diagnostic_correct:
                                raise ValueError("Device diagnostic result failed correctness validation")
                            device_profile = parse_device_profile(diagnostic["profiler_csv"], capture=capture)
                        except (ValueError, TypeError, KeyError) as error:
                            device_profile = {"available": False, "valid": False, "reason": str(error)}
                        device_profile["artifacts"] = [filename]
                        device_profile["artifact_sha256"] = sha256(diagnostic["profiler_csv"].encode())
                        device_profile["instrumented"] = True
                        device_profile["telemetry"] = diagnostic.get("telemetry", [])
                    else:
                        device_profile = {"available": False, "valid": False, "reason": "device_profiling_failed_or_no_csv", "diagnostic_status": diagnostic["status"]}
                        if filename:
                            device_profile.update({"artifacts": [filename], "artifact_sha256": sha256(diagnostic["profiler_csv"].encode())})
                    attempt.event("pass_completed", case_id=index, pass_name="device", status=diagnostic["status"],
                                  comparison=profile_case["correctness"]["device_comparison"], capture_valid=device_profile.get("valid", False))
                profile_case["device"] = device_profile
                host_recipe_matches = not host_profile.get("available") or (host_profile.get("warmup_count") == self.policy["host"]["warmup_count"] and host_profile.get("sample_count") == self.policy["host"]["sample_count"])
                capture = device_profile.get("capture", {})
                device_recipe_matches = not device_profile.get("available") or (capture.get("warmup") == self.policy["device"]["warmup_count"] and capture.get("sample_count") == self.policy["device"]["sample_count"])
                profile_case["policy_validation"] = {"matches": host_recipe_matches and device_recipe_matches,
                    "host_recipe_matches": host_recipe_matches, "device_recipe_matches": device_recipe_matches,
                    "note": "Observed metadata only; submitted Python shares the wrapper process and is not tamper-attested"}
                profile_case.update({"state": "terminal", "finished_at": datetime.now(timezone.utc).isoformat(), "finished_monotonic_ns": time.monotonic_ns()})
                report["quality"] = quality_report(report["cases"])
                report["environment"]["after"] = telemetry_snapshot()
                attempt.event("case_completed", case_id=index, status=status)
                attempt.checkpoint()
                if host_profile.get("available"):
                    result["message"] += f" Host synchronized median: {host_profile['statistics']['median_ns'] / 1000:.3f} μs ({host_profile['sample_count']} samples)."
                results.append(result)
                score += result["score"]
                if status != AC:
                    final_status = status
                await self.send(rid, "next", case=result, progress=round(index * 100 / len(cases)))
                if status in (CE, TLE, MLE, OLE, RE):
                    break
            final = {"score": round(score, 2), "time": round(sum(r["time"] for r in results), 2),
                     "memory": max(r["memory"] for r in results),
                     "message": "Time sums per-case container wall time including TT initialization/JIT/transfers. Memory is sampled host memory, excluding device memory."}
            attempt.write_private("hydro-cases.json", (json.dumps(results, ensure_ascii=False, indent=2) + "\n").encode())
            await end(final_status, "evaluation_completed", **final)
            log.info("Finished record=%s attempt=%s status=%s score=%s", rid, attempt.id, final_status, round(score, 2))
        except asyncio.CancelledError:
            if self.active:
                await asyncio.to_thread(self.active.stop)
                await asyncio.to_thread(self.active.remove)
                self.active = None
            if attempt and not attempt.finalized:
                for case in attempt.report["cases"]:
                    if case.get("state") == "running":
                        case.update({"state": "interrupted", "interruption_reason": "task_cancelled"})
                attempt.finish({"status": None, "score": 0, "message": "Evaluation cancelled after disconnect or worker shutdown."},
                               "task_cancelled", quality_report(attempt.report["cases"]))
            raise
        except Exception as error:
            log.exception("Judge error for %s", rid)
            if self.active:
                await asyncio.to_thread(self.active.stop)
                await asyncio.to_thread(self.active.remove)
                self.active = None
            if attempt and not attempt.finalized:
                for case in attempt.report["cases"]:
                    if case.get("state") == "running":
                        case.update({"state": "interrupted", "interruption_reason": "infrastructure_error"})
                with contextlib.suppress(Exception):
                    await end(SE, "infrastructure_error", message=f"Judge infrastructure error: {error}")
            elif not attempt:
                with contextlib.suppress(Exception):
                    await self.send(rid, "end", status=SE, score=0, message=f"Judge archive initialization failed: {error}")
        finally:
            self.attempt = None

    async def connection(self):
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True), headers={"Accept": "application/json"},
                                         timeout=aiohttp.ClientTimeout(total=60)) as session:
            self.http = session
            sid = await self.login()
            ws_url = BASE_URL.replace("http://", "ws://").replace("https://", "wss://") + "judge/conn"
            async with session.ws_connect(ws_url, headers={"Authorization": f"Bearer {sid}"}, max_msg_size=16 * 1024 * 1024) as ws:
                self.ws = ws
                await ws.send_json({"key": "config", "lang": list(LANGS), "concurrency": 1, "type": ["judge"]})
                await ws.send_json({"key": "start"})
                log.info("Connected to Hydro; serial hardware worker ready")
                (JOBS / "worker-status.json").write_text(json.dumps({"connected": True, "host": socket.gethostname(), "languages": LANGS}))
                async for message in ws:
                    if message.type == aiohttp.WSMsgType.TEXT:
                        if message.data == "ping":
                            await ws.send_str("pong")
                            continue
                        request = json.loads(message.data)
                        if request.get("task"):
                            if self.judging and not self.judging.done():
                                raise RuntimeError("Server sent simultaneous hardware tasks despite concurrency=1")
                            self.judging = asyncio.create_task(self.judge(request["task"]))
                    elif message.type == aiohttp.WSMsgType.ERROR:
                        raise RuntimeError(f"Judge websocket error: {ws.exception()}")
            if self.judging and not self.judging.done():
                self.judging.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.judging

    async def run(self):
        while not self.shutting_down:
            try:
                await self.connection()
            except asyncio.CancelledError:
                if self.judging and not self.judging.done():
                    self.judging.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self.judging
                raise
            except Exception:
                log.exception("Hydro connection failed; reconnecting")
                if self.judging and not self.judging.done():
                    self.judging.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self.judging
            (JOBS / "worker-status.json").write_text(json.dumps({"connected": False}))
            await asyncio.sleep(5)


async def main():
    judge = Judge()
    task = asyncio.create_task(judge.run())
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, task.cancel)
    with contextlib.suppress(asyncio.CancelledError):
        await task


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main())
