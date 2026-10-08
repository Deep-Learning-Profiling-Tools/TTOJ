FROM tt-oj-profiler-runtime:0.79.0
COPY judge-src/ /opt/tt-oj/src/
COPY judge-kernels/ /opt/tt-oj/kernels/
RUN cmake -S /opt/tt-oj/src -B /opt/tt-oj/build -G Ninja \
      -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/opt/tt-oj \
      -DCMAKE_CXX_COMPILER=clang++-20 -DTT_OJ_DEVICE_PROFILER_SUPPORTED=ON \
    && cmake --build /opt/tt-oj/build --parallel 2 \
    && cmake --install /opt/tt-oj/build \
    && rm -rf /opt/tt-oj/build
COPY judge-entrypoint.sh /opt/tt-oj/entrypoint.sh
RUN chmod 755 /opt/tt-oj/entrypoint.sh
USER 1005:1005
WORKDIR /tmp
ENTRYPOINT ["/opt/tt-oj/entrypoint.sh"]
