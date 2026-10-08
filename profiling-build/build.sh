#!/usr/bin/env bash
set -euo pipefail
task_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
oj_dir=$(dirname -- "$task_dir")
base_image=ghcr.io/tenstorrent/tt-metal/tt-metalium-ubuntu-22.04-release-amd64@sha256:ed828284c1b95eaaaac784aa777df16338d3788baa6af971c13aa4789fb75b5c
source_commit=de546d3b146758714d900f11b218c8f9c805f410

# The desktop session may predate membership of the docker group. %q quotes
# every argument for the shell invoked by sg, including filesystem paths.
docker_cmd() {
    local shell_command
    printf -v shell_command '%q ' docker "$@"
    sg docker -c "$shell_command"
}

if [[ ! -d "$task_dir/tt-metal/.git" ]]; then
    git clone --depth 1 --branch v0.79.0 https://github.com/tenstorrent/tt-metal.git "$task_dir/tt-metal"
fi
if [[ $(git -C "$task_dir/tt-metal" rev-parse HEAD) != "$source_commit" ]]; then
    printf 'Unexpected source commit; expected %s\n' "$source_commit" >&2
    exit 1
fi
git -C "$task_dir/tt-metal" submodule update --init --recursive --depth 1
docker_cmd run --rm --entrypoint /bin/bash -v "$task_dir:/workspace" \
    "$base_image" /workspace/compile.sh
docker_cmd build --progress=plain -f "$task_dir/runtime.Dockerfile" \
    -t tt-oj-profiler-runtime:0.79.0 "$task_dir"
mkdir -p "$task_dir/judge-src" "$task_dir/judge-kernels"
cp "$oj_dir/sandbox/metalium/"{CMakeLists.txt,judge-riscv.cpp,judge-compute.cpp,profiling.hpp} "$task_dir/judge-src/"
cp "$oj_dir/sandbox/metalium/kernels/"*.cpp "$task_dir/judge-kernels/"
cp "$oj_dir/sandbox/metalium/entrypoint.sh" "$task_dir/judge-entrypoint.sh"
docker_cmd build --progress=plain -f "$task_dir/judge.Dockerfile" \
    -t tt-oj-metalium-profiler:0.79.0 "$task_dir"
