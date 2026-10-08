FROM ubuntu:24.04

ARG METALIUM_VERSION=0.79.0~ubuntu24.04
ARG SFPI_VERSION=7.83.0
ENV DEBIAN_FRONTEND=noninteractive
COPY tt-pkg-key.asc /etc/apt/keyrings/tt-pkg-key.asc
RUN printf '%s\n' 'deb [signed-by=/etc/apt/keyrings/tt-pkg-key.asc] https://ppa.tenstorrent.com/ubuntu/ noble main' > /etc/apt/sources.list.d/tenstorrent.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       cmake ninja-build g++ libhwloc15 libevent-core-2.1-7t64 libevent-pthreads-2.1-7t64 \
       tt-metalium=${METALIUM_VERSION} tt-metalium-dev=${METALIUM_VERSION} \
       tt-metalium-examples=${METALIUM_VERSION} sfpi=${SFPI_VERSION} \
    && rm -rf /var/lib/apt/lists/*

# Metalium's distributed API references ULFM symbols from official OpenMPI 5.
# These are from the official Tenstorrent release runtime, scoped to this image.
COPY mpi/ /opt/tt-oj/mpi/
COPY kernels/ /opt/tt-oj/kernels/
COPY CMakeLists.txt judge-riscv.cpp judge-compute.cpp profiling.hpp /opt/tt-oj/src/
RUN cmake -S /opt/tt-oj/src -B /opt/tt-oj/build -G Ninja \
      -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/opt/tt-oj -DTT_OJ_DEVICE_PROFILER_SUPPORTED=OFF \
    && cmake --build /opt/tt-oj/build --parallel 2 \
    && cmake --install /opt/tt-oj/build \
    && rm -rf /opt/tt-oj/build \
    && groupadd -g 1005 judge \
    && useradd -u 1005 -g 1005 -M -d /tmp/tt-home -s /usr/sbin/nologin judge \
    && mkdir -p /workspace

COPY entrypoint.sh /opt/tt-oj/entrypoint.sh
RUN chmod 755 /opt/tt-oj/entrypoint.sh
ENV LD_LIBRARY_PATH=/opt/tt-oj/mpi \
    HOME=/tmp/tt-home \
    XDG_CACHE_HOME=/tmp/tt-home/.cache \
    TT_METAL_LOGS_PATH=/tmp/tt-logs \
    TT_METAL_PROFILER_DIR=/tmp/generated/profiler
USER 1005:1005
WORKDIR /tmp
ENTRYPOINT ["/opt/tt-oj/entrypoint.sh"]
