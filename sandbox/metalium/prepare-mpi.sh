#!/usr/bin/env bash
set -euo pipefail
metalium_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
mkdir -p "$metalium_dir/mpi"
runtime_image=ghcr.io/tenstorrent/tt-metal/tt-metalium-ubuntu-22.04-release-amd64@sha256:ed828284c1b95eaaaac784aa777df16338d3788baa6af971c13aa4789fb75b5c
docker run --rm --network none --user "$(id -u):$(id -g)" \
    -v "$metalium_dir/mpi:/out" --entrypoint /bin/sh "$runtime_image" \
    -c 'cp -L /opt/openmpi-v5.0.7-ulfm/lib/libmpi.so.40 /opt/openmpi-v5.0.7-ulfm/lib/libopen-pal.so.80 /opt/openmpi-v5.0.7-ulfm/lib/libpmix.so.2 /out/'
