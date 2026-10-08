"""Create reproducible TTNN addition/matmul problem fixtures, without Torch."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def matrix(rows, cols, offset=0):
    return [[((r * cols + c + offset) % 9 - 4) / 2 for c in range(cols)] for r in range(rows)]


def make_problem(name, pid, operation, tests):
    folder = ROOT / "problems" / name
    folder.mkdir(parents=True, exist_ok=True)
    example = f"import ttnn\n\ndef solve(a, b):\n    return ttnn.{operation}(a, b)\n"
    starter = "import ttnn\n\ndef solve(a, b):\n    # a, b: device 0 TTNN tensors. Return a device tensor.\n    raise NotImplementedError\n"
    starter_zh = "import ttnn\n\ndef solve(a, b):\n    # a、b 已在 device 0 上；返回设备张量。\n    raise NotImplementedError\n"
    symbol = "逐元素加法 A + B" if operation == "add" else "矩阵乘法 A × B"
    description = f"""在 Tenstorrent Blackhole 上实现 **{symbol}**。

提交一个 Python 文件，定义 `solve(a, b)`。评测器已创建 TTNN BF16 设备张量，调用你的函数，再将结果读回主机，与参考答案逐元素比较。

`a` 和 `b` 都是 `ttnn.Tensor`，位于 **device 0**、使用 `ttnn.TILE_LAYOUT`。返回值必须是设备上的 TTNN Tensor。你不需要自己打开或关闭设备，也不需要读 stdin、打印答案。

```python
{starter_zh.rstrip()}
```

学习示例：

```python
{example.rstrip()}
```

数据包含多个 32 的倍数的矩阵形状、负数、零和小数。结果形状也参与校验，绝对误差上限 0.015625，相对误差上限 0.005。

第一版评测 **正确性**：每测试点 120 秒墙钟限制、8 GB 主机内存限制。网页时间包含设备初始化、JIT 编译与数据传输，不能作为纯 kernel 延迟。所有硬件任务串行运行。请仅使用这道题分配的 device 0。

如需下载起始代码，查看题目附件 `starter.py`。

评测记录中的 **Profiling / 性能剖析** 将热身后的重复测量与容器墙钟时间分别显示，包括中位数、P95、样本数和抖动。设备时间只有在设备 profiler 实际提供 trace 时才显示；并行 stage 的耗时不能直接相加。

本平台用于可信提交的本地实验。Python 提交与包装器运行在同一进程，设备 Tensor 类型检查不构成强制硬件执行或防作弊证明。
"""
    symbol_en = "elementwise addition A + B" if operation == "add" else "matrix multiplication A × B"
    description_en = f"""Implement **{symbol_en}** on Tenstorrent Blackhole.

Submit a Python file defining `solve(a, b)`. The wrapper creates BF16 device tensors, calls your function, reads the result back to the host, and compares every element against the reference.

Both inputs are `ttnn.Tensor` objects on **device 0**, with `ttnn.TILE_LAYOUT`. Return a device TTNN Tensor. Do not open or close the device yourself, read stdin, or print an answer.

```python
{starter.rstrip()}
```

Example:

```python
{example.rstrip()}
```

The tests cover matrix shapes that are multiples of 32, negative values, zeros, and fractions. Output shape is checked. Absolute tolerance is 0.015625 and relative tolerance is 0.005.

Correctness evaluation allows 120 seconds of wall time per testcase and 8 GB of host memory. The record's total wall time includes device initialization, JIT compilation, and transfers; it is not pure kernel latency. Hardware jobs run serially. Use only the assigned device 0.

Download `starter.py` from the problem attachments if needed.

The submission's **Profiling** panel reports repeated measurements after warmup separately from container wall time, including median, P95, sample count, and jitter. Device timing is displayed only when the device profiler produces a trace. Parallel stage durations must not be summed.

This platform is for local experiments with trusted submissions. Python code shares a process with its wrapper; device Tensor type checks do not prove hardware execution or prevent cheating.
"""
    existing = folder / "metadata.json"
    metadata = json.loads(existing.read_text()) if existing.exists() else {}
    metadata.update({
        "id": pid, "title": f"TTNN · {symbol}",
        "titles": {"zh": f"TTNN · {symbol}", "en": f"TTNN · {symbol_en[0].upper() + symbol_en[1:]}"},
        "content": json.dumps({"zh": description, "en": description_en}, ensure_ascii=False),
        "language": "ttnn", "mode": "ttnn", "time_ms": 120000,
        "atol": 0.015625, "rtol": 0.005, "tests": tests,
    })
    (folder / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
    (folder / "accepted.py").write_text(example)
    (folder / "starter.py").write_text(starter)
    (folder / "wrong.py").write_text("def solve(a, b):\n    return a\n")
    (folder / "compile-error.py").write_text("def solve(a, b)\n    return a\n")
    (folder / "runtime-error.py").write_text("def solve(a, b):\n    raise RuntimeError('Intentional test runtime failure')\n")


def main():
    addition = []
    for rows, cols, offset in ((32, 32, 3), (32, 64, 5), (64, 32, 1)):
        a, b = matrix(rows, cols), matrix(rows, cols, offset)
        expected = [[x + y for x, y in zip(arow, brow)] for arow, brow in zip(a, b)]
        addition.append({"input": json.dumps({"a": a, "b": b}) + "\n", "output": json.dumps(expected) + "\n"})
    make_problem("ttnn-add", "TT1001", "add", addition)
    multiplication = []
    for m, k, n, offset in ((32, 32, 32, 1), (32, 64, 32, 4), (64, 32, 32, 2)):
        a, b = matrix(m, k), matrix(k, n, offset)
        expected = [[sum(a[r][j] * b[j][c] for j in range(k)) for c in range(n)] for r in range(m)]
        multiplication.append({"input": json.dumps({"a": a, "b": b}) + "\n", "output": json.dumps(expected) + "\n"})
    make_problem("ttnn-matmul", "TT1002", "matmul", multiplication)


if __name__ == "__main__":
    main()
