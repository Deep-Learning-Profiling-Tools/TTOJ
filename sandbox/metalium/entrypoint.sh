#!/bin/sh
set -eu
mkdir -p "$HOME" "$XDG_CACHE_HOME" "$TT_METAL_LOGS_PATH" "$TT_METAL_PROFILER_DIR"
exec "$@"
