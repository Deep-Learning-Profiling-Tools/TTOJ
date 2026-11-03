"""Hydro judge adapter with fixed, isolated Tenstorrent execution recipes.

The broker has Docker access. Submitted programs never receive the socket,
credentials, expected outputs, host home directory, or a network connection.
TT-Lang jobs each take one Blackhole chip from the pool in TT_OJ_CHIPS, so several run in
parallel; the ttnn/Metalium runtimes discover the whole mesh and take every pooled chip.
A TT-Lang submission uses two containers: one judges every testcase (correctness, host and
device timing), a second reads the hardware counters of the accepted testcases.
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
from metrics import numeric_block, quality_report, sha256, summarize_profile, telemetry_snapshot
from device_profile import parse_device_profile
from perf_counters import DEFAULT_MASK as COUNTER_MASK, parse_perf_counters, summarize_counters
from protocol import Attempt, load_policy, recover_interrupted

AC, WA, TLE, MLE, OLE, RE, CE, SE = range(1, 9)
LANGS = ("ttnn", "ttlang", "metalium-riscv", "metalium-compute")
PYTHON_MODES = ("ttnn", "ttlang")
MARKER = "TT_OJ_RESULT:"
PROFILE_MARKER = "TT_OJ_PROFILE:"
DETAIL_MARKER = "TT_OJ_DETAIL:"
OUTPUT_LIMIT = 256 * 1024
# A batched TT-Lang container carries the compiler output of every testcase (about 120 KB per case for the
# softmax reference, mostly profiler pragma notes), so its limit scales with the number of testcases.
BATCH_OUTPUT_PER_CASE = 512 * 1024
BASE_URL = os.environ.get("HYDRO_URL", "http://site:8888/").rstrip("/") + "/"
JOBS = Path(os.environ.get("JOBS_DIR", "/jobs"))
HOST_JOBS = Path(os.environ["JOBS_HOST_DIR"])
TTNN_IMAGE = os.environ["TTNN_IMAGE"]
METALIUM_IMAGE = os.environ.get("METALIUM_IMAGE", "tt-oj-metalium:0.1")
TTLANG_IMAGE = os.environ.get("TTLANG_IMAGE", "tt-oj-ttlang-runtime:1.1.10")
RUNTIME_IMAGES = {"ttnn": TTNN_IMAGE, "ttlang": TTLANG_IMAGE, "metalium": METALIUM_IMAGE}


def image_family(mode):
    return mode if mode in PYTHON_MODES else "metalium"
log = logging.getLogger("tt-judge")
PROFILES = Path(os.environ.get("PROFILES_DIR", "/profiles"))
BENCHMARK_CPUSET = os.environ.get("BENCHMARK_CPUSET", "4-7,12-15")
DEVICE_PROFILER_SUPPORTED = os.environ.get("TT_OJ_DEVICE_PROFILER_SUPPORTED") == "1"
DEVICE_CSV_PATH = "/tmp/generated/profiler/.logs/profile_log_device.csv"
DEVICE_ARTIFACT_LIMIT = 64 * 1024 * 1024  # a 110-core TT-Lang kernel writes ~7 MB per case


def parse_chips(text):
    chips = sorted({int(item) for item in text.split(",") if item.strip()})
    if not chips or any(not 0 <= chip < 64 for chip in chips):
        raise ValueError("TT_OJ_CHIPS must list /dev/tenstorrent indices, for example 0,1,2")
    return chips


# Chips the judge may use; the others stay free for interactive users of the machine.
CHIPS = parse_chips(os.environ.get("TT_OJ_CHIPS", "0,1,2,3"))
CHIP_COUNT = len(CHIPS)
WORKER_CONCURRENCY = max(1, min(CHIP_COUNT, int(os.environ.get("TT_OJ_CONCURRENCY", str(CHIP_COUNT)))))
CASE_START_MARKER = "TT_OJ_CASE_START:"
CASE_DONE_MARKER = "TT_OJ_CASE:"
CASES_DIR = "/tmp/tt-oj-cases"
COMPILE_FAILURE = re.compile(r"TTLangCompileError|^error: ", re.M)
# A container that sees a single chip is a "CUSTOM" cluster for tt-metal, which then requires an
# explicit mesh graph descriptor; the wheel bundles this one-chip Blackhole (p150) descriptor.
TTLANG_MESH_DESCRIPTOR = ("/opt/venv/lib/python3.12/site-packages/ttnn/tt_metal/fabric/"
                          "mesh_graph_descriptors/p150_mesh_graph_descriptor.textproto")


class ChipPool:
    """Hands out exclusive Tenstorrent chips to concurrent jobs."""
    def __init__(self, chips):
        self.free = set(chips)
        self.condition = asyncio.Condition()

    async def acquire(self, count):
        async with self.condition:
            await self.condition.wait_for(lambda: len(self.free) >= count)
            chips = sorted(self.free)[:count]
            self.free.difference_update(chips)
            return chips

    async def release(self, chips):
        async with self.condition:
            self.free.update(chips)
            self.condition.notify_all()


def case_output_matches(result, expected_text, atol, rtol):
    """None when the case has no reference, else whether the wrapper's result matches it."""
    if expected_text is None:
        return None
    try:
        return bool(compare_numbers(result, json.loads(expected_text), atol, rtol))
    except (ValueError, TypeError):
        return False


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


def batch_progress(stdout):
    """Case ids that the TT-Lang wrapper has started (in order) and finished, from its stdout bytes."""
    started, finished = [], set()
    for line in bytes(stdout).splitlines():
        for marker, bucket in ((CASE_START_MARKER, started), (CASE_DONE_MARKER, finished)):
            if line.startswith(marker.encode()):
                try:
                    case_id = json.loads(line[len(marker):]).get("id")
                except (ValueError, AttributeError):
                    break
                if type(case_id) is int:
                    if isinstance(bucket, list):
                        if case_id not in bucket:
                            bucket.append(case_id)
                    else:
                        bucket.add(case_id)
                break
    return started, finished


def batch_payloads(stdout, ids):
    """Per-case payload lines of the TT-Lang wrapper; more than one line per case is tampering."""
    payloads = {}
    for line in stdout.splitlines():
        if not line.startswith(CASE_DONE_MARKER):
            continue
        try:
            value = json.loads(line[len(CASE_DONE_MARKER):])
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("id") in ids:
            payloads.setdefault(value["id"], []).append(value)
    return payloads


class Execution:
    """A container lifecycle that can be stopped on timeout/disconnect/shutdown."""
    def __init__(self, client, container, profiling=False, attempt=None, case_id=None, pass_name=None, artifact_cases=None,
                 output_limit=OUTPUT_LIMIT):
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
        self.pass_name = pass_name or ("device" if profiling else "host")
        # Batched TT-Lang passes leave one CSV per testcase in CASES_DIR instead of one CSV per container.
        self.artifact_cases = list(artifact_cases) if artifact_cases else None
        self.output_limit = output_limit  # a batched container carries every testcase's output
        self.expire_requested = False
        self.salvaged = None
        self.reader = threading.Thread(target=self._read, daemon=True)

    def _read(self):
        try:
            for stdout, stderr in self.container.attach(stream=True, logs=True, demux=True):
                for data, target in ((stdout, self.out), (stderr, self.err)):
                    if data:
                        remaining = self.output_limit - len(self.out) - len(self.err)
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

    def expire(self):
        """Ask the run loop to end the container with TLE (called from the event loop thread)."""
        self.expire_requested = True

    def collect_case_artifacts(self):
        """Per-testcase CSVs written by the batched wrapper; paths are derived from the case ids, never from stdout."""
        collected = {}
        for case_id in self.artifact_cases:
            text = self.collect_profiler(f"{CASES_DIR}/case-{case_id}-{self.pass_name}.csv")
            if text is not None:
                collected[case_id] = text
        return collected

    def salvage(self):
        """Before killing a batched container, keep the CSVs of the testcases that already finished."""
        if self.profiling and self.artifact_cases and self.salvaged is None:
            try:
                self.salvaged = self.collect_case_artifacts()
            except docker.errors.DockerException:
                self.salvaged = {}

    def collect_profiler(self, path=DEVICE_CSV_PATH):
        """Read inside the live mount namespace; Docker archive misses tmpfs."""
        request = self.client.api.exec_create(self.container.id,
            ["/usr/bin/head", "-c", str(DEVICE_ARTIFACT_LIMIT + 1), path],
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
        profiler_csvs = None
        artifact_collected = False
        last_snapshot = 0
        while True:
            elapsed = time.monotonic() - start
            if self.output_exceeded:
                status = OLE
                self.stop()
                break
            if elapsed > timeout or self.expire_requested:
                status = TLE
                self.salvage()
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
                        if self.artifact_cases:
                            profiler_csvs = self.collect_case_artifacts()
                        elif int(completed.group(1)) == 0:
                            profiler_csv = self.collect_profiler()
                    finally:
                        self.acknowledge_profiler()
                    artifact_collected = True
            try:
                memory = self.container.stats(stream=False, one_shot=True).get("memory_stats", {}).get("usage", 0)
                self.peak_memory = max(self.peak_memory, memory)
                if time.monotonic() - last_snapshot >= 1:
                    self.telemetry.append(telemetry_snapshot())
                    last_snapshot = time.monotonic()
            except docker.errors.APIError:
                pass
            time.sleep(0.15)
        self.reader.join(timeout=3)
        self.container.reload()
        state = self.container.attrs["State"]
        if state.get("OOMKilled"):
            status = MLE
        # Recover any output emitted before the streaming attachment connected. The json-file log is
        # size-capped and rotated, so a long batch loses its head there: keep the longer capture.
        stdout = self.container.logs(stdout=True, stderr=False)
        stderr = self.container.logs(stdout=False, stderr=True)
        if len(stdout) + len(stderr) > self.output_limit:
            self.output_exceeded = True
        if len(stdout) < len(self.out):
            stdout = bytes(self.out)
        if len(stderr) < len(self.err):
            stderr = bytes(self.err)
        self.out = bytearray(stdout[:self.output_limit])
        self.err = bytearray(stderr[:max(0, self.output_limit - len(self.out))])
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
            "profiler_csvs": profiler_csvs if profiler_csvs is not None else self.salvaged,
        }


class Judge:
    def __init__(self):
        self.docker = docker.from_env()
        self.http = None
        self.ws = None
        self.jobs = {}
        self.chips = ChipPool(CHIPS)
        self.send_lock = asyncio.Lock()
        self.shutting_down = False
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
        async with self.send_lock:  # several jobs share one judge websocket
            await self.ws.send_json({"rid": rid, "key": key, **payload})

    async def cancel_jobs(self):
        jobs = [job for job in self.jobs.values() if not job.done()]
        for job in jobs:
            job.cancel()
        for job in jobs:
            with contextlib.suppress(asyncio.CancelledError):
                await job
        self.jobs = {}

    def prepare(self, folder, task, manifest, case, profiling=False, counters=False, attempt=None, chips=None, batch_cases=None):
        if batch_cases:
            pass_name = "counters" if counters else "profile" if profiling else "host"
        else:
            pass_name = "counters" if counters else "device" if profiling else "host"
        folder.mkdir(parents=True, exist_ok=True)
        mode = manifest["mode"]
        source_name = "submission.py" if mode in PYTHON_MODES else "solution.cpp"
        (folder / source_name).write_text(task["code"], encoding="utf-8")
        (folder / "input.txt").write_text(case["input"], encoding="utf-8")
        if mode in PYTHON_MODES:
            (folder / "runner.py").write_bytes(Path(f"/app/{mode}_runner.py").read_bytes())
        host_folder = HOST_JOBS / folder.relative_to(JOBS)
        chips = list(chips if chips is not None else CHIPS)
        single_chip = mode == "ttlang" and len(chips) == 1
        if single_chip:
            # The reserved chip is mapped to node 0, so the wrapper's device_id=0 opens exactly that chip.
            devices = [f"/dev/tenstorrent/{chips[0]}:/dev/tenstorrent/0:rwm"]
        else:
            # The ttnn/Metalium runtimes discover the whole mesh; they hold every chip while they run.
            devices = [f"/dev/tenstorrent/{i}:/dev/tenstorrent/{i}:rwm" for i in chips]
        executable = "/opt/venv/bin/python3 /workspace/runner.py" if mode in PYTHON_MODES else (
            "/opt/tt-oj/bin/judge-riscv" if mode == "metalium-riscv" else "/opt/tt-oj/bin/judge-compute"
        )
        # Resolve a mutable tag once per attempt; every pass executes that ID.
        image = task.get("_runtime_image_id") or RUNTIME_IMAGES[image_family(mode)]
        environment = {
            "HOME": "/tmp", "PYTHONUNBUFFERED": "1", "TT_METAL_LOGGER_LEVEL": "warning",
            "TT_OJ_BENCHMARK": "0" if profiling else "1",
            "TT_OJ_WARMUP": str(self.policy["host"]["warmup_count"]),
            "TT_OJ_SAMPLES": str(self.policy["host"]["sample_count"]),
            "TT_OJ_PROFILE_ONLY": "1" if profiling and not counters else "0",
            "TT_OJ_COUNTERS_ONLY": "1" if counters else "0",
            "TT_OJ_DEVICE_PROFILER_SUPPORTED": "1" if DEVICE_PROFILER_SUPPORTED else "0",
            "TT_METAL_PROFILER_DIR": "/tmp/generated/profiler",
        }
        if mode == "ttlang":
            # The TT-Lang wheel bundles its own runtime; its perf tools look under $TT_METAL_HOME/generated/profiler.
            environment["TT_METAL_HOME"] = "/tmp"
            if single_chip:
                environment["TT_MESH_GRAPH_DESC_PATH"] = TTLANG_MESH_DESCRIPTOR
        else:
            environment["TT_METAL_RUNTIME_ROOT"] = "/opt/tt-profiler/libexec/tt-metalium"
        if counters:
            # Tensix hardware counters (fpu, pack, unpack, instruction threads) read after each kernel.
            environment["TT_METAL_PROFILE_PERF_COUNTERS"] = str(COUNTER_MASK)
        if profiling:
            environment["TT_METAL_DEVICE_PROFILER"] = "1"
            # NoC event tracing stays off: it logs every transfer (65 MB of CSV for one large multicore
            # case) and perturbs the kernel time that the TT-Lang leaderboard ranks.
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
            # The cap must exceed the output limit (a batched TT-Lang container carries every testcase's
            # compiler output), or the rotated log loses the first testcases' result lines.
            log_config=LogConfig(type="json-file", config={"max-size": "32m", "max-file": "1"}),
            labels={"tt-oj.worker": "local", "tt-oj.record": task["rid"],
                    "tt-oj.attempt": attempt.id if attempt else "unknown"},
        )
        case_id = int(folder.name.split("-")[0])
        if attempt:
            try:
                attempt.event("container_created", case_id=case_id,
                              pass_name=pass_name, container_id=container.id, chips=chips,
                              runtime_image_id=image, cpu_affinity=BENCHMARK_CPUSET)
            except Exception:
                with contextlib.suppress(docker.errors.NotFound, docker.errors.APIError):
                    container.remove(force=True)
                raise
        return Execution(self.docker, container, profiling=profiling, attempt=attempt, case_id=case_id, pass_name=pass_name,
                         artifact_cases=batch_cases, output_limit=BATCH_OUTPUT_PER_CASE * len(batch_cases) if batch_cases else OUTPUT_LIMIT)

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

    async def prepare_execution(self, folder, task, manifest, case, profiling=False, counters=False, attempt=None, chips=None, batch_cases=None):
        # Cancellation must not abandon a still-running Docker create thread.
        preparing = asyncio.create_task(asyncio.to_thread(self.prepare, folder, task, manifest, case, profiling, counters, attempt, chips, batch_cases))
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

    async def run_batch(self, rid, task, manifest, attempt, chips, pass_name, items, per_case_timeout, progress=None):
        """Run one container over several TT-Lang testcases.

        Returns the container outcome, the case ids the wrapper started (in order) and the payload
        lines it printed, keyed by case id. A testcase still running per_case_timeout seconds after
        its start marker ends the container with TLE; the CSVs of finished cases are kept.
        """
        ids = [item["id"] for item in items]
        recipe = self.policy["counters" if pass_name == "counters" else "device"]
        batch = {"pass": pass_name, "warmup": recipe["warmup_count"], "samples": recipe["sample_count"], "cases": items}
        attempt.event("pass_prepare_started", case_id=0, pass_name=pass_name, case_ids=ids)
        execution = await self.prepare_execution(attempt.private / f"0-{pass_name}", task, manifest,
            {"input": json.dumps(batch), "output": None}, profiling=pass_name != "host",
            counters=pass_name == "counters", attempt=attempt, chips=chips, batch_cases=ids)
        running = asyncio.create_task(self.run_execution(execution, per_case_timeout * len(ids)))
        first_seen, completed, loop_started = {}, set(), time.monotonic()
        try:
            while True:
                done, _ = await asyncio.wait({running}, timeout=1.0)
                if done:
                    break
                started, finished = batch_progress(execution.out)
                for case_id in started:
                    first_seen.setdefault(case_id, time.monotonic())
                if finished - completed:
                    completed |= finished
                    if progress:
                        await progress(len(completed & set(ids)), len(ids))
                current = started[-1] if started else None
                if current not in finished and not execution.expire_requested:
                    if time.monotonic() - first_seen.get(current, loop_started) > per_case_timeout:
                        attempt.event("case_timeout", case_id=current, pass_name=pass_name, limit_s=per_case_timeout)
                        execution.expire()
        except asyncio.CancelledError:
            running.cancel()
            with contextlib.suppress(BaseException):
                await running
            raise
        outcome = await running
        started, _ = batch_progress(outcome["stdout"].encode("utf-8", "replace"))
        attempt.event("pass_completed", case_id=0, pass_name=pass_name, status=outcome["status"],
                      started_case_ids=started, wall_time_ms=outcome["time"])
        return outcome, started, batch_payloads(outcome["stdout"], set(ids))

    async def judge_ttlang(self, rid, task, manifest, cases, timeout, attempt, chips):
        """Judge a TT-Lang submission: every testcase in one container, then counters in a second."""
        report = attempt.report
        report["execution_limits"].update({"per_case_wall_timeout_s": timeout, "batch_wall_timeout_s": timeout * len(cases),
                                           "output_capture_bytes": BATCH_OUTPUT_PER_CASE * len(cases),
                                           "pass_structure": "profile_batch_then_counters_batch"})
        report["passes"] = {}
        atol, rtol = manifest.get("atol", 0), manifest.get("rtol", 0)
        items, profile_cases = [], {}
        for index, case in enumerate(cases, 1):
            try:
                spec = json.loads(case["input"])
            except ValueError as error:
                raise ValueError(f"TT-Lang testcase {index} input is not JSON: {error}")
            if not isinstance(spec, dict):
                raise ValueError(f"TT-Lang testcase {index} input must be a JSON object")
            items.append({"id": index, "spec": spec})
            profile_cases[index] = {"id": index, "state": "queued", "status": None,
                "correctness": {"reference_available": case["output"] is not None,
                    "input_sha256": sha256(case["input"].encode()),
                    "expected_output_sha256": sha256(case["output"].encode()) if case["output"] is not None else None,
                    "atol": atol, "rtol": rtol, "host_comparison": "not_run", "device_comparison": "not_run",
                    "repeat_check_scope": "first_and_final_not_every_invocation"},
                "host": {"available": False, "reason": "not_run"}, "device": {"available": False, "reason": "not_run"},
                "counters": {"available": False, "reason": "not_run"}, "telemetry_ref": None}
            report["cases"].append(profile_cases[index])
        attempt.checkpoint()
        pass_name = "profile" if DEVICE_PROFILER_SUPPORTED else "host"
        await self.send(rid, "next", status=20, progress=0,
                        message=f"Running {len(cases)} testcases on one chip: correctness, host and device timing.")

        async def profile_progress(completed, total):
            await self.send(rid, "next", status=20, progress=round(completed * 90 / total),
                            message=f"Testcase {completed} / {total} finished.")

        batch_started = datetime.now(timezone.utc).isoformat()
        outcome, started, payloads = await self.run_batch(rid, task, manifest, attempt, chips, pass_name, items, timeout, profile_progress)
        report["passes"][pass_name] = {"case_ids": [item["id"] for item in items], "status": outcome["status"],
            "exit_code": outcome.get("exit_code"), "wall_time_ms": outcome["time"], "memory_kib": outcome["memory"],
            "started_case_ids": started, "telemetry": outcome.get("telemetry", []), "started_at": batch_started}
        csvs = outcome.get("profiler_csvs") or {}
        full_log = outcome["stderr"] + "\n" + outcome["stdout"]
        results, final_status, score = [], AC, 0
        for index, case in enumerate(cases, 1):
            profile_case = profile_cases[index]
            lines = payloads.get(index, [])
            payload = lines[0] if len(lines) == 1 and isinstance(lines[0], dict) else None
            status, message, comparison = None, "", "not_run"
            profile = payload.get("profile") if payload and isinstance(payload.get("profile"), dict) else {}
            if payload is not None and "error" not in payload:
                result = payload.get("result")
                try:
                    encoded = json.dumps(result)
                except (TypeError, ValueError):
                    encoded = None
                if encoded is None:
                    status, message, comparison = RE, "Invalid result format.", "invalid_result_format"
                elif case["output"] is None:
                    status, message, comparison = AC, "Self-test output: " + encoded[:2000], "self_test_no_reference"
                else:
                    matches = compare_numbers(result, json.loads(case["output"]), atol, rtol)
                    status = AC if matches else WA
                    message = "All elements passed validation." if matches else "Output values or tensor shape differ from the reference."
                    comparison = "pass" if matches else "fail"
                if encoded is not None:
                    profile_case["correctness"]["actual_output_sha256"] = sha256(encoded.encode())
                if status in (AC, WA) and isinstance(payload.get("detail"), str):
                    message += " " + payload["detail"][:1000]
            elif payload is not None:
                error_text = str(payload.get("error", ""))[-4000:]
                status = CE if COMPILE_FAILURE.search(error_text) else RE
                message, comparison = error_text, "wrapper_exception"
                if status == CE:
                    await self.send(rid, "next", compilerText=error_text[-10000:])
            elif len(lines) > 1:
                status, message, comparison = RE, "Program produced more than one result for this testcase.", "duplicate_result_marker"
            elif index in started or (index == 1 and not started):
                # The container ended while this testcase was running (or before the first one started).
                status = outcome["status"] or RE
                if status == TLE:
                    message = f"Exceeded the {timeout:g}s per-testcase wall limit including JIT; container terminated."
                elif status == MLE:
                    message = "Exceeded the 8 GiB host memory limit."
                elif status == OLE:
                    message = f"stdout/stderr exceeded {BATCH_OUTPUT_PER_CASE * len(cases) // 1024} KiB over the whole submission (compiler output included)."
                else:
                    status = CE if COMPILE_FAILURE.search(full_log) else RE
                    message = full_log[-4000:]
                    if status == CE:
                        await self.send(rid, "next", compilerText=full_log[-10000:])
                comparison = "no_result_marker"
            else:
                break  # an earlier testcase ended the run; this one was never started
            profile_case["correctness"]["host_comparison"] = comparison
            wall_ns = payload.get("wall_ns") if payload else None
            wall_ms = round(wall_ns / 1e6, 2) if type(wall_ns) in (int, float) and math.isfinite(wall_ns) and wall_ns >= 0 else outcome["time"]
            host_profile = summarize_profile(profile) if status == AC else {"available": False, "reason": "not_timed_non_accepted_case"}
            profile_case["correctness"]["host_repeatability_verified"] = host_profile.get("repeatability_verified")
            if status != AC:
                device_profile = {"available": False, "reason": "skipped_non_accepted_case"}
            elif pass_name != "profile":
                device_profile = {"available": False, "reason": "profiler_enabled_sdk_required"}
            elif csvs.get(index):
                csv_text = csvs[index]
                filename = attempt.write_csv(index, csv_text)
                try:
                    device_profile = parse_device_profile(csv_text, capture=profile.get("device_capture"))
                    for block in ("traffic", "correctness"):
                        if isinstance(profile.get(block), dict):
                            device_profile[block] = numeric_block(profile[block])
                except (ValueError, TypeError, KeyError) as error:
                    device_profile = {"available": False, "valid": False, "reason": str(error)}
                device_profile.update({"artifacts": [filename], "artifact_sha256": sha256(csv_text.encode()), "instrumented": True,
                                       "same_run_as_correctness": True})
                # Correctness, host samples and device cycles come from one run of the same tensors.
                profile_case["correctness"]["device_comparison"] = comparison
                profile_case["correctness"]["device_repeatability_verified"] = host_profile.get("repeatability_verified")
            else:
                device_profile = {"available": False, "valid": False, "reason": "device_csv_not_collected", "diagnostic_status": outcome["status"]}
            result = {"id": index, "subtaskId": 1, "status": status, "score": 100 / len(cases) if status == AC else 0,
                      "time": wall_ms, "memory": outcome["memory"], "message": message}
            if host_profile.get("available"):
                result["message"] += f" Host synchronized median: {host_profile['statistics']['median_ns'] / 1000:.3f} μs ({host_profile['sample_count']} samples)."
            recipe = self.policy["device"]
            host_recipe_matches = not host_profile.get("available") or (host_profile.get("warmup_count") == recipe["warmup_count"] and host_profile.get("sample_count") == recipe["sample_count"])
            capture = device_profile.get("capture", {}) if isinstance(device_profile.get("capture"), dict) else {}
            device_recipe_matches = not device_profile.get("available") or (capture.get("warmup") == recipe["warmup_count"] and capture.get("sample_count") == recipe["sample_count"])
            profile_case.update({"state": "running", "status": status, "wall_time_ms": wall_ms, "host": host_profile, "device": device_profile,
                "telemetry_ref": pass_name, "runtime_affinity_warning": "Unable to bind" in full_log,
                "policy_validation": {"matches": host_recipe_matches and device_recipe_matches,
                    "host_recipe_matches": host_recipe_matches, "device_recipe_matches": device_recipe_matches,
                    "note": "Observed metadata only; submitted Python shares the wrapper process and is not tamper-attested"}})
            attempt.event("case_completed", case_id=index, status=status, comparison=comparison)
            results.append(result)
            score += result["score"]
            if status != AC:
                final_status = status
            await self.send(rid, "next", case=result, progress=round(90 + index * 5 / len(cases)))
            if status in (CE, TLE, MLE, OLE, RE):
                break
        attempt.checkpoint()
        accepted = [item for item in items if profile_cases[item["id"]]["status"] == AC]
        counters = {}
        counters_outcome = None
        if accepted and DEVICE_PROFILER_SUPPORTED:
            await self.send(rid, "next", status=20, progress=95,
                            message=f"Reading hardware performance counters for {len(accepted)} accepted testcases.")
            counters_started = datetime.now(timezone.utc).isoformat()
            counters_outcome, counters_started_ids, counters = await self.run_batch(rid, task, manifest, attempt, chips, "counters", accepted, timeout)
            report["passes"]["counters"] = {"case_ids": [item["id"] for item in accepted], "status": counters_outcome["status"],
                "exit_code": counters_outcome.get("exit_code"), "wall_time_ms": counters_outcome["time"], "memory_kib": counters_outcome["memory"],
                "started_case_ids": counters_started_ids, "telemetry": counters_outcome.get("telemetry", []), "started_at": counters_started}
        counter_csvs = (counters_outcome or {}).get("profiler_csvs") or {}
        for item in items:
            index = item["id"]
            profile_case = profile_cases[index]
            if profile_case["state"] == "queued":
                profile_case.update({"state": "not_run", "reason": "earlier_testcase_ended_the_run"})
                continue
            if profile_case["status"] != AC:
                counters_profile = {"available": False, "reason": "skipped_non_accepted_case"}
            elif not DEVICE_PROFILER_SUPPORTED:
                counters_profile = {"available": False, "reason": "not_collected"}
            else:
                lines = counters.get(index, [])
                payload = lines[0] if len(lines) == 1 and isinstance(lines[0], dict) else None
                csv_text = counter_csvs.get(index)
                if payload and "error" not in payload and csv_text:
                    filename = attempt.write_csv(index, csv_text, kind="counters")
                    profile = payload.get("profile") if isinstance(payload.get("profile"), dict) else {}
                    try:
                        if case_output_matches(payload.get("result"), cases[index - 1]["output"], atol, rtol) is False:
                            raise ValueError("Counter pass result failed correctness validation")
                        parsed = parse_perf_counters(csv_text, COUNTER_MASK, capture=profile.get("counter_capture"))
                        counters_profile = {key: parsed.get(key) for key in ("available", "valid", "mask", "groups", "clock_mhz", "cores",
                                                                             "windows_decoded", "window_us_median", "counters", "errors", "notes")}
                        counters_profile["summary"] = summarize_counters(parsed)
                        counters_profile["capture"] = profile.get("counter_capture")
                    except (ValueError, TypeError, KeyError) as error:
                        counters_profile = {"available": False, "valid": False, "reason": str(error)}
                    counters_profile["artifacts"] = [filename]
                else:
                    counters_profile = {"available": False, "valid": False, "reason": "counter_pass_failed_or_no_csv",
                                        "diagnostic_status": (counters_outcome or {}).get("status")}
                    if payload and isinstance(payload.get("error"), str):
                        counters_profile["error_tail"] = payload["error"][-1000:]
            profile_case["counters"] = counters_profile
            profile_case.update({"state": "terminal", "finished_at": datetime.now(timezone.utc).isoformat()})
        report["quality"] = quality_report(report["cases"])
        report["environment"]["after"] = telemetry_snapshot()
        attempt.checkpoint()
        return results, final_status, score

    async def judge(self, task):
        rid = str(task.get("rid", ""))
        if not re.fullmatch(r"[0-9a-fA-F]{24}", rid):
            log.error("Rejected task with invalid record id")
            return
        task = dict(task)
        task["rid"] = rid = rid.lower()
        attempt = None
        active = None
        chips = None
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
            attempt = Attempt(JOBS, PROFILES, task)
            report = attempt.report
            report["scheduler"] = {"concurrency": WORKER_CONCURRENCY, "reservation": None, "device_ids": None,
                "pool_device_ids": list(CHIPS), "discovered_devices": list(CHIPS), "cpu_affinity": BENCHMARK_CPUSET,
                "cpu_quota": None, "queue_priority": task.get("priority"),
                "queue_policy": "Hydro priority queue", "external_contention_verified": False}
            code_paths = [Path(__file__), Path(__file__).with_name("metrics.py"), Path(__file__).with_name("device_profile.py"),
                          Path(__file__).with_name("protocol.py"), Path('/app/ttnn_runner.py'), Path('/app/ttlang_runner.py')]
            code_paths.extend(sorted(Path('/app/reference').glob('*')))
            report["environment"].update({
                "ttnn_wrapper_sha256": sha256(Path('/app/ttnn_runner.py').read_bytes()),
                "ttlang_wrapper_sha256": sha256(Path('/app/ttlang_runner.py').read_bytes()),
                "worker_code_sha256": {path.name: sha256(path.read_bytes()) for path in code_paths if path.is_file()},
                "sdk_commit": "de546d3b146758714d900f11b218c8f9c805f410",
                "sdk_build": "v0.79.0 ReleaseTracy; profiler enabled only in separate diagnostic pass",
                "runtime_images": dict(RUNTIME_IMAGES),
                "before": telemetry_snapshot(), "host_kernel": os.uname().release,
                "worker_hostname": socket.gethostname(), "image_ids": {},
            })
            report["environment"].update({name: sha256(path.read_bytes()) for name, path in (
                ("worker_sha256", Path(__file__)), ("metrics_sha256", Path(__file__).with_name("metrics.py")),
                ("device_parser_sha256", Path(__file__).with_name("device_profile.py")),
                ("protocol_sha256", Path(__file__).with_name("protocol.py")))})
            attempt.checkpoint()
            await self.send(rid, "next", status=21, message="Preparing Tenstorrent containers.")
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
            if mode in PYTHON_MODES:
                attempt.event("syntax_check_started")
                try:
                    compile(task["code"], "submission.py", "exec")
                except SyntaxError as error:
                    await end(CE, "python_syntax_error", compilerText=str(error))
                    return
                attempt.event("syntax_check_passed")
            images = {name: self.docker.images.get(image) for name, image in RUNTIME_IMAGES.items()}
            report["environment"]["image_ids"] = {name: image.id for name, image in images.items()}
            report["environment"]["image_repo_digests"] = {name: image.attrs.get("RepoDigests", []) for name, image in images.items()}
            task["_runtime_image_id"] = images[image_family(mode)].id
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
            needed = 1 if mode == "ttlang" else CHIP_COUNT
            if len(self.chips.free) < needed:
                await self.send(rid, "next", message="Waiting for a free Tenstorrent chip.")
            chips = await self.chips.acquire(needed)
            report["scheduler"].update({"reservation": "single_chip" if needed == 1 else "whole_mesh", "device_ids": chips})
            attempt.event("chips_acquired", chips=chips)
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
            if mode == "ttlang":
                # One container judges every testcase, a second reads the counters; the per-case
                # loop below is for the ttnn/Metalium modes, which run one container per pass.
                results, final_status, score = await self.judge_ttlang(rid, task, manifest, cases, timeout, attempt, chips)
            for index, case in enumerate([] if mode == "ttlang" else cases, 1):
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
                active = await self.prepare_execution(folder, task, manifest, case, attempt=attempt, chips=chips)
                try:
                    outcome = await self.run_execution(active, timeout)
                finally:
                    active = None
                status, message = outcome["status"], ""
                full_log = outcome["stderr"] + "\n" + outcome["stdout"]
                compile_failure = (mode == "ttlang" and re.search(r"TTLangCompileError|^error: ", full_log, re.M)) or (
                    mode not in PYTHON_MODES and re.search(r"(?:error:|Build failed|compilation failed|JIT compilation|CompileError)", full_log, re.I))
                if status == RE and compile_failure:
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
                if mode == "ttlang" and status in (AC, WA):
                    # The TT-Lang wrapper compares against the reference itself; surface its one-line summary.
                    details = [line[len(DETAIL_MARKER):] for line in outcome["stdout"].splitlines() if line.startswith(DETAIL_MARKER)]
                    if len(details) == 1:
                        message += " " + details[0][:1000]
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
                    active = await self.prepare_execution(attempt.private / f"{index}-device", task, manifest, case, profiling=True, attempt=attempt, chips=chips)
                    try:
                        diagnostic = await self.run_execution(active, timeout)
                    finally:
                        active = None
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
                            for block in ("traffic", "correctness"):
                                if isinstance(metadata.get(block), dict):
                                    device_profile[block] = numeric_block(metadata[block])
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
                counters_profile = {"available": False, "reason": "skipped_non_accepted_case" if status != AC else "not_collected"}
                if status == AC and DEVICE_PROFILER_SUPPORTED and mode == "ttlang":
                    await self.send(rid, "next", message=f"Case {index}: reading hardware performance counters.")
                    attempt.event("pass_prepare_started", case_id=index, pass_name="counters")
                    active = await self.prepare_execution(attempt.private / f"{index}-counters", task, manifest, case, profiling=True, counters=True, attempt=attempt, chips=chips)
                    try:
                        diagnostic = await self.run_execution(active, timeout)
                    finally:
                        active = None
                    filename = attempt.write_csv(index, diagnostic["profiler_csv"], kind="counters") if diagnostic.get("profiler_csv") else None
                    if diagnostic["status"] is None and filename:
                        capture_lines = [line[len(PROFILE_MARKER):] for line in diagnostic["stdout"].splitlines() if line.startswith(PROFILE_MARKER)]
                        diagnostic_results = [line[len(MARKER):] for line in diagnostic["stdout"].splitlines() if line.startswith(MARKER)]
                        try:
                            metadata = json.loads(capture_lines[0]) if len(capture_lines) == 1 else {}
                            correct = len(diagnostic_results) == 1 and (case["output"] is None or compare_numbers(
                                json.loads(diagnostic_results[0]), json.loads(case["output"]), manifest.get("atol", 0), manifest.get("rtol", 0)))
                            if not correct:
                                raise ValueError("Counter pass result failed correctness validation")
                            parsed = parse_perf_counters(diagnostic["profiler_csv"], COUNTER_MASK, capture=metadata.get("counter_capture"))
                            counters_profile = {key: parsed.get(key) for key in ("available", "valid", "mask", "groups", "clock_mhz", "cores",
                                                                                 "windows_decoded", "window_us_median", "counters", "errors", "notes")}
                            counters_profile["summary"] = summarize_counters(parsed)
                            counters_profile["capture"] = metadata.get("counter_capture")
                        except (ValueError, TypeError, KeyError) as error:
                            counters_profile = {"available": False, "valid": False, "reason": str(error)}
                        counters_profile["artifacts"] = [filename]
                    else:
                        counters_profile = {"available": False, "valid": False, "reason": "counter_pass_failed_or_no_csv", "diagnostic_status": diagnostic["status"]}
                    attempt.event("pass_completed", case_id=index, pass_name="counters", status=diagnostic["status"], counters_valid=counters_profile.get("valid", False))
                profile_case["counters"] = counters_profile
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
                     "message": ("Time sums per-testcase wall time inside the shared container, including JIT and transfers "
                                 "but not container start or device initialization. Memory is sampled host memory, excluding device memory."
                                 if mode == "ttlang" else
                                 "Time sums per-case container wall time including TT initialization/JIT/transfers. Memory is sampled host memory, excluding device memory.")}
            attempt.write_private("hydro-cases.json", (json.dumps(results, ensure_ascii=False, indent=2) + "\n").encode())
            await end(final_status, "evaluation_completed", **final)
            log.info("Finished record=%s attempt=%s status=%s score=%s", rid, attempt.id, final_status, round(score, 2))
        except asyncio.CancelledError:
            if active:
                await asyncio.to_thread(active.stop)
                await asyncio.to_thread(active.remove)
                active = None
            if attempt and not attempt.finalized:
                for case in attempt.report["cases"]:
                    if case.get("state") == "running":
                        case.update({"state": "interrupted", "interruption_reason": "task_cancelled"})
                attempt.finish({"status": None, "score": 0, "message": "Evaluation cancelled after disconnect or worker shutdown."},
                               "task_cancelled", quality_report(attempt.report["cases"]))
            raise
        except Exception as error:
            log.exception("Judge error for %s", rid)
            if active:
                await asyncio.to_thread(active.stop)
                await asyncio.to_thread(active.remove)
                active = None
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
            if chips is not None:
                await self.chips.release(chips)

    async def connection(self):
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True), headers={"Accept": "application/json"},
                                         timeout=aiohttp.ClientTimeout(total=60)) as session:
            self.http = session
            sid = await self.login()
            ws_url = BASE_URL.replace("http://", "ws://").replace("https://", "wss://") + "judge/conn"
            async with session.ws_connect(ws_url, headers={"Authorization": f"Bearer {sid}"}, max_msg_size=16 * 1024 * 1024) as ws:
                self.ws = ws
                await ws.send_json({"key": "config", "lang": list(LANGS), "concurrency": WORKER_CONCURRENCY, "type": ["judge"]})
                await ws.send_json({"key": "start"})
                log.info("Connected to Hydro; chips %s, concurrency %s", CHIPS, WORKER_CONCURRENCY)
                (JOBS / "worker-status.json").write_text(json.dumps({"connected": True, "host": socket.gethostname(), "languages": LANGS}))
                async for message in ws:
                    if message.type == aiohttp.WSMsgType.TEXT:
                        if message.data == "ping":
                            await ws.send_str("pong")
                            continue
                        request = json.loads(message.data)
                        if request.get("task"):
                            self.jobs = {key: job for key, job in self.jobs.items() if not job.done()}
                            if len(self.jobs) >= WORKER_CONCURRENCY:
                                raise RuntimeError(f"Server sent more than {WORKER_CONCURRENCY} simultaneous tasks")
                            key = f"{request['task'].get('rid')}:{uuid.uuid4().hex[:8]}"
                            self.jobs[key] = asyncio.create_task(self.judge(request["task"]))
                    elif message.type == aiohttp.WSMsgType.ERROR:
                        raise RuntimeError(f"Judge websocket error: {ws.exception()}")
            await self.cancel_jobs()

    async def run(self):
        while not self.shutting_down:
            try:
                await self.connection()
            except asyncio.CancelledError:
                await self.cancel_jobs()
                raise
            except Exception:
                log.exception("Hydro connection failed; reconnecting")
                await self.cancel_jobs()
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
