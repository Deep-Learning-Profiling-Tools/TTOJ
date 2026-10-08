FROM ghcr.io/tenstorrent/tt-metal/tt-metalium-ubuntu-22.04-release-amd64@sha256:ed828284c1b95eaaaac784aa777df16338d3788baa6af971c13aa4789fb75b5c

# Built from the unmodified v0.79.0 tag with ENABLE_TRACY=ON. The old
# runtime wheel has profiler stubs, so replace it instead of overlaying it.
COPY install/opt/tt-profiler/ /opt/tt-profiler/
COPY wheels/ /tmp/tt-profiler-wheels/
RUN uv pip install --python /opt/venv/bin/python --reinstall --no-deps /tmp/tt-profiler-wheels/*.whl \
    && rm -rf /tmp/tt-profiler-wheels \
    && groupadd -g 1005 judge \
    && useradd -u 1005 -g 1005 -M -d /tmp/tt-home -s /usr/sbin/nologin judge \
    && mkdir -p /opt/tt-oj /workspace /opt/tt-profiler/libexec/tt-metalium/build/profiler/build_wasm/traces \
    && ln -s /opt/openmpi-v5.0.7-ulfm/lib /opt/tt-oj/mpi

ENV CMAKE_PREFIX_PATH=/opt/tt-profiler \
    TT_METAL_HOME=/opt/tt-profiler/libexec/tt-metalium \
    TT_METAL_RUNTIME_ROOT=/opt/tt-profiler/libexec/tt-metalium \
    LD_LIBRARY_PATH=/opt/tt-profiler/lib:/opt/openmpi-v5.0.7-ulfm/lib \
    HOME=/tmp/tt-home \
    XDG_CACHE_HOME=/tmp/tt-home/.cache \
    TT_METAL_LOGS_PATH=/tmp/tt-logs \
    TT_METAL_PROFILER_DIR=/tmp/generated/profiler \
    TT_METAL_PROFILER_DISABLE_PUSH_TO_TRACY=1 \
    TT_METAL_PROFILER_CPP_POST_PROCESS=0
COPY provenance.json /opt/tt-oj/profiler-provenance.json
WORKDIR /tmp
ENTRYPOINT []
