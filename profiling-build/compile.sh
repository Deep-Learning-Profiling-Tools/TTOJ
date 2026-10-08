#!/usr/bin/env bash
set -euo pipefail
git config --global --add safe.directory /workspace/tt-metal
cd /workspace/tt-metal
./build_metal.sh --use-system-sfpi --install-prefix /opt/tt-profiler \
    --build-dir /workspace/build --configure-only --disable-light-metal-trace \
    > /workspace/configure.log 2>&1
cmake --build /workspace/build --parallel "${TT_OJ_BUILD_JOBS:-16}" > /workspace/build.log 2>&1
DESTDIR=/workspace/install cmake --install /workspace/build > /workspace/install.log 2>&1
# setup.py's official precompiled path expects the libraries in build/lib.
cp -a /workspace/install/opt/tt-profiler/lib/*.so* /workspace/build/lib/
uv pip install --python /opt/venv/bin/python \
    setuptools==80.10.2 setuptools_scm==8.1.0 wheel==0.45.1 \
    > /workspace/wheel-dependencies.log 2>&1
TT_FROM_PRECOMPILED_DIR=/workspace/tt-metal python setup.py bdist_wheel \
    --dist-dir /workspace/wheels > /workspace/wheel.log 2>&1
