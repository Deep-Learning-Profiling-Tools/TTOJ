FROM python:3.12-slim
WORKDIR /app
COPY worker/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY worker/*.py ./
COPY worker/evaluation-policy.json ./
COPY sandbox/ttnn/runner.py /app/ttnn_runner.py
COPY sandbox/ttlang/runner.py /app/ttlang_runner.py
COPY sandbox/metalium/judge-riscv.cpp sandbox/metalium/judge-compute.cpp sandbox/metalium/profiling.hpp sandbox/metalium/CMakeLists.txt /app/reference/
CMD ["python", "-u", "worker.py"]
