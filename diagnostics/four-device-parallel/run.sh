#!/usr/bin/env bash
set -euo pipefail
oj_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$oj_root"
tt_proof_container="tt-oj-four-device-proof-$$"
restore_worker() {
    sg docker -c "docker rm -f '$tt_proof_container'" >/dev/null 2>&1 || true
    exec 9>&-
    sg docker -c 'docker compose start worker' >/dev/null
}
trap restore_worker EXIT
sg docker -c 'docker compose stop worker'
exec 9<data/jobs/hardware.lock
flock -n 9
timeout 180s sg docker -c "docker run --rm --name '$tt_proof_container' \
    --network none --read-only --user 1005:1005 --cpuset-cpus 4-7,12-15 \
    --cap-drop ALL --security-opt no-new-privileges:true --memory 8g --pids-limit 512 \
    --tmpfs /tmp:rw,exec,size=2147483648,mode=1777 \
    --device /dev/tenstorrent/0 --device /dev/tenstorrent/1 \
    --device /dev/tenstorrent/2 --device /dev/tenstorrent/3 \
    -v "$oj_root/diagnostics/four-device-parallel:/workspace:ro" \
    -e HOME=/tmp -e TT_METAL_RUNTIME_ROOT=/opt/tt-profiler/libexec/tt-metalium \
    -e TT_METAL_DEVICE_PROFILER=0 -e TT_METAL_LOGGER_LEVEL=warning \
    --entrypoint /bin/sh tt-oj-metalium-profiler:0.79.0 \
    -c 'cmake -S /workspace -B /tmp/build -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_COMPILER=clang++-20 && cmake --build /tmp/build -j4 && /tmp/build/four-device-parallel'" \
    >diagnostics/four-device-parallel/run.log 2>&1
python3 - <<'PY'
import json
from pathlib import Path
folder = Path('diagnostics/four-device-parallel')
lines = folder.joinpath('run.log').read_text().splitlines()
results = [json.loads(line.split(':', 1)[1]) for line in lines if line.startswith('TT_OJ_PARALLEL_RESULT:')]
assert len(results) == 1
folder.joinpath('result.json').write_text(json.dumps(results[0], indent=2))
print(json.dumps(results[0], indent=2))
PY
