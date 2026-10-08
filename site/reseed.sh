#!/usr/bin/env bash
set -euo pipefail
oj_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$oj_directory"
# The mounted problem manifests are read again at each site start.
if docker info >/dev/null 2>&1; then
    docker compose restart site
else
    sg docker -c 'docker compose restart site'
fi
