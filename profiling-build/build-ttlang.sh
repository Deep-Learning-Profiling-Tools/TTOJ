#!/usr/bin/env bash
# Build the TT-Lang runtime image used by the CS540 problems (TT3001-TT3003).
set -euo pipefail
task_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
tag=${TTLANG_IMAGE:-tt-oj-ttlang-runtime:1.1.10}

docker_cmd() {
    local shell_command
    printf -v shell_command '%q ' docker "$@"
    if docker info >/dev/null 2>&1; then docker "$@"; else sg docker -c "$shell_command"; fi
}

docker_cmd build --progress=plain -f "$task_dir/ttlang.Dockerfile" -t "$tag" "$task_dir"
# Smoke-test the device from the new image before pointing the worker at it.
docker_cmd run --rm --device /dev/tenstorrent/0 --device /dev/tenstorrent/1 \
    --device /dev/tenstorrent/2 --device /dev/tenstorrent/3 --user 1005:1005 \
    --tmpfs /tmp:rw,exec,size=2147483648,mode=1777 "$tag" \
    /opt/venv/bin/python -c 'import ttnn; d = ttnn.open_device(device_id=0); ttnn.close_device(d); print("device ok")'
