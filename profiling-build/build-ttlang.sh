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

# Keep a local copy of the pinned wheel next to the Dockerfile (gitignored) so the build does not
# depend on Tenstorrent's package index staying up; verify it against the recorded checksum.
wheel=tt_lang-1.1.10.dev20260921-cp312-cp312-manylinux_2_39_x86_64.whl
wheel_url="https://pypi.eng.aws.tenstorrent.com/tt-lang/$wheel"
mkdir -p "$task_dir/wheels"
if [ ! -f "$task_dir/wheels/$wheel" ]; then
    echo "Downloading $wheel_url"
    curl -fsSL -o "$task_dir/wheels/$wheel.part" "$wheel_url" && mv "$task_dir/wheels/$wheel.part" "$task_dir/wheels/$wheel"
fi
(cd "$task_dir/wheels" && sha256sum -c "$task_dir/wheels.sha256")

docker_cmd build --progress=plain -f "$task_dir/ttlang.Dockerfile" -t "$tag" "$task_dir"
# Smoke-test one chip the way the worker runs it: a single node mapped to 0 with the one-chip mesh descriptor.
docker_cmd run --rm --device /dev/tenstorrent/0:/dev/tenstorrent/0 --user 1005:1005 \
    --tmpfs /tmp:rw,exec,size=2147483648,mode=1777 \
    -e TT_MESH_GRAPH_DESC_PATH=/opt/venv/lib/python3.12/site-packages/ttnn/tt_metal/fabric/mesh_graph_descriptors/p150_mesh_graph_descriptor.textproto \
    "$tag" /opt/venv/bin/python -c 'import ttnn; d = ttnn.open_device(device_id=0); ttnn.close_device(d); print("device ok")'
# Keep an exportable copy of the image for machines without index access: docker load -i <file>.
mkdir -p "$task_dir/../data/images"
docker_cmd save "$tag" | gzip -1 > "$task_dir/../data/images/${tag//[:\/]/-}.tar.gz.part" \
    && mv "$task_dir/../data/images/${tag//[:\/]/-}.tar.gz.part" "$task_dir/../data/images/${tag//[:\/]/-}.tar.gz"
