"""Run inside the broker image; verify cancellation using real Docker jobs.

No accelerator devices are passed to these test containers. The delay during
prepare reproduces the Docker create/cancellation race found during review.
"""
import asyncio
import contextlib
import json
import os
import time
import uuid

import docker
from worker import Execution, Judge, TLE, compare_numbers


async def main():
    client = docker.from_env()
    label = "tt-oj.cleanup-check=" + uuid.uuid4().hex
    names = []
    judge = Judge.__new__(Judge)

    def create(command="sleep 20", profiling=False):
        container = client.containers.create(
            os.environ["TTNN_IMAGE"], ["-c", command], entrypoint="/bin/sh",
            network_mode="none", read_only=True, cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"], user="1005:1005",
            cpuset_cpus="0-3,8-11",
            tmpfs={"/tmp": "rw,size=16777216,mode=1777"},
            labels={label.split("=", 1)[0]: label.split("=", 1)[1]},
        )
        names.append(container.id)
        return Execution(client, container, profiling=profiling)

    def slow_prepare(*_):
        time.sleep(0.15)
        return create()

    try:
        judge.prepare = slow_prepare
        pending = asyncio.create_task(judge.prepare_execution(None, None, None, None))
        await asyncio.sleep(0.03)
        pending.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pending
        assert not client.containers.list(all=True, filters={"label": label}), "Preparation cancellation leaked a container"

        execution = create()
        pending = asyncio.create_task(judge.run_execution(execution, 30))
        for _ in range(100):
            execution.container.reload()
            if execution.container.attrs["State"]["Running"]:
                break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("Cancellation check container did not start")
        pending.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pending
        assert not client.containers.list(all=True, filters={"label": label}), "Running cancellation leaked a container"

        execution = create()
        original_run = execution.run
        def delayed_start(timeout):
            time.sleep(0.15)
            return original_run(timeout)
        execution.run = delayed_start
        start = time.monotonic()
        pending = asyncio.create_task(judge.run_execution(execution, 30))
        await asyncio.sleep(0.03)
        pending.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pending
        assert time.monotonic() - start < 3, "Start cancellation waited for the job timeout"
        assert not client.containers.list(all=True, filters={"label": label}), "Start cancellation leaked a container"

        result = await judge.run_execution(create(), 0.25)
        assert result["status"] == TLE
        assert not client.containers.list(all=True, filters={"label": label}), "Timeout leaked a container"
        # The file lives in tmpfs and must be archived before the shell exits.
        result = await judge.run_execution(create(
            "mkdir -p /tmp/generated/profiler/.logs; "
            "printf 'profiler fixture\\n' > /tmp/generated/profiler/.logs/profile_log_device.csv; "
            "printf 'TT_OJ_DEVICE_CAPTURE_DONE:0\\n'; "
            "while [ ! -f /tmp/tt-oj-artifact-ack ]; do sleep 0.05; done; exit 0", profiling=True), 10)
        assert result["status"] is None and result["exit_code"] == 0, {k:result[k] for k in ("status","exit_code","stdout","stderr")}
        assert result["profiler_csv"] == "profiler fixture\n", "Tmpfs profiler artifact was lost"
        result = await judge.run_execution(create(
            "printf 'TT_OJ_DEVICE_CAPTURE_DONE:1\\n'; "
            "while [ ! -f /tmp/tt-oj-artifact-ack ]; do sleep 0.05; done; exit 1", profiling=True), 10)
        assert result["status"] == 6 and result["exit_code"] == 1, "Diagnostic exit status was not preserved"
        assert not compare_numbers(570, 579), "Integer comparison accepted wrong result"
        assert not compare_numbers([1], [[1]]), "Tensor comparison ignored shape"
        assert compare_numbers([1.01], [1.0], atol=0.02)
        print(json.dumps({"prepare_cancel": "pass", "running_cancel": "pass", "start_cancel": "pass", "timeout_cleanup": "pass", "tmpfs_profiler_capture": "pass", "diagnostic_exit_status": "pass", "numeric_comparison": "pass"}))
    finally:
        for container in client.containers.list(all=True, filters={"label": label}):
            container.remove(force=True)


if __name__ == "__main__":
    asyncio.run(main())
