#!/usr/bin/env bash
set -euo pipefail
oj_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$oj_root"
export TTOJ_SOURCE_REF=${TTOJ_SOURCE_REF:-$(git rev-parse HEAD 2>/dev/null || printf main)}

docker_cmd() {
    if docker info >/dev/null 2>&1; then
        docker "$@"
    else
        # Group membership may predate this shell. Quote every argument before
        # requesting a fresh docker-group shell; no sudo password is needed.
        local docker_command=docker docker_argument docker_quoted
        for docker_argument in "$@"; do
            printf -v docker_quoted '%q' "$docker_argument"
            docker_command+=" $docker_quoted"
        done
        sg docker -c "$docker_command"
    fi
}

case "${1:-status}" in
    up)
        if ! docker_cmd image inspect tt-oj-profiler-runtime:0.79.0 >/dev/null 2>&1 \
            || ! docker_cmd image inspect tt-oj-metalium-profiler:0.79.0 >/dev/null 2>&1; then
            "$oj_root/profiling-build/build.sh"
        fi
        docker_cmd compose up -d --build
        ;;
    build-runtimes) "$oj_root/profiling-build/build.sh" ;;
    stop) docker_cmd compose stop ;;
    down) docker_cmd compose down ;;
    status) docker_cmd compose ps ;;
    logs) docker_cmd compose logs --tail 80 "${2:-worker}" ;;
    reseed)
        docker_cmd compose exec -T site hydrooj cli execute 'await require("/app/bootstrap.cjs")({ reseedProblems: true }).catch((e) => { console.error(e); process.exit(1); })'
        docker_cmd compose restart site
        ;;
    credentials)
        python3 - <<'PY'
from pathlib import Path
config = dict(line.split('=', 1) for line in Path('.env').read_text().splitlines() if '=' in line and not line.startswith('#'))
print('URL: http://127.0.0.1:8888/')
print('Username:', config['ADMIN_USERNAME'])
print('Password:', config['ADMIN_PASSWORD'])
PY
        ;;
    *) printf 'Usage: %s {up|build-runtimes|stop|down|status|logs [service]|reseed|credentials}\n' "$0" >&2; exit 2 ;;
esac
