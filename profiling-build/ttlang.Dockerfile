# TT-Lang execution runtime for the CS540 exercises.
#
# The tt-lang wheel bundles its own ttnn/tt-metal runtime (built with the device
# profiler enabled), so this image does not reuse the tt-metal profiler build.
# Pin the exact wheel: ``==1.1.10.dev20260921`` would also match the ``+light``
# (no-ttnn) wheel on Tenstorrent's index, so the full wheel URL is used.
# manylinux_2_39 wheel: needs glibc 2.39, i.e. Ubuntu 24.04 (matches the host OS); Python 3.12.
FROM ubuntu:24.04

ARG TTLANG_WHEEL_URL=https://pypi.eng.aws.tenstorrent.com/tt-lang/tt_lang-1.1.10.dev20260921-cp312-cp312-manylinux_2_39_x86_64.whl
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.12 python3.12-venv libpython3.12 libgomp1 libnuma1 libhwloc15 libatomic1 zlib1g \
        xz-utils ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && python3.12 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --extra-index-url "${TORCH_INDEX_URL}" "${TTLANG_WHEEL_URL}" \
    && /opt/venv/bin/tt-lang-setup-sfpi \
    && groupadd -g 1005 judge \
    && useradd -u 1005 -g 1005 -M -d /tmp/tt-home -s /usr/sbin/nologin judge \
    && mkdir -p /opt/tt-oj /workspace \
    && /opt/venv/bin/python -c "import ttl, ttnn; print('tt-lang', ttl.__version__)" > /opt/tt-oj/ttlang-version.txt

ENV PATH=/opt/venv/bin:$PATH \
    HOME=/tmp/tt-home \
    XDG_CACHE_HOME=/tmp/tt-home/.cache \
    TT_METAL_LOGS_PATH=/tmp/tt-logs \
    TT_METAL_PROFILER_DIR=/tmp/generated/profiler \
    TT_METAL_PROFILER_DISABLE_PUSH_TO_TRACY=1 \
    TT_METAL_PROFILER_CPP_POST_PROCESS=0 \
    TT_METAL_HOME=/tmp
WORKDIR /tmp
ENTRYPOINT []
