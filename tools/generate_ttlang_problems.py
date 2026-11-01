"""Create the CS540 TT-Lang exercise problems (TT3001-TT3003) from the course test-case schedule.

Inputs are never stored in the problem: each test is a small JSON spec (shape + seed) and the
trusted wrapper (sandbox/ttlang/runner.py) regenerates the same tensors, pads them, runs the
submitted TT-Lang operation on device 0 and compares against the PyTorch reference in-process.
The expected "output" of every test is the literal verdict marker the wrapper prints on success.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEED = 540
SUBMISSION_LIMIT = 200
TIME_MS = 180000
VERDICT = json.dumps({"verdict": "pass"}) + "\n"

VECTOR_LENGTHS = [1, 31, 33, 1023, 1024, 1025, 65537, 1048577, 16777216, 16777217]
RAGGED_CASES = [(256, 65), (8192, 96), (1024, 542), (1024, 8192), (32, 16384), (7, 17), (33, 1), (127, 37), (4097, 257), (1, 8193)]

# Elementwise check: |got - want| <= atol + rtol * scale. The FPU path rounds FP32 operands to
# ~11 significant bits (TF32-like), so rtol is set with roughly 5-10x margin over the observed
# error of the reference kernels on a Blackhole p300c (tt-lang 1.1.10.dev20260921).
TOLERANCES = {
    "vector-add": {"atol": 1e-6, "rtol": 2 ** -8, "scale": "|a| + |b| per element"},
    "ragged-sum": {"atol": 1e-6, "rtol": 2 ** -8, "scale": "sum of |x| over the valid prefix of the row"},
    "ragged-softmax": {"atol": 1e-4, "rtol": 2 ** -6, "scale": "|reference value| per element"},
}

COMMON_EN = """
## How the judge runs your code

The judge imports your file and calls the operation named **`{entry}`** exactly as shown in the signature; it
never calls anything else. Inputs are already **FP32, tile-layout tensors in device DRAM** on device 0 and the
output tensor `out` is preallocated with zeros. Do not open or close the device, do not read stdin and do not
print the answer. Only the TT-Lang operation runs on hardware; host-side code in your file runs once at import.

{harness}

Correctness: every logical output element must satisfy `|got - want| <= {atol:g} + {rtol:g} x scale`, where
scale is {scale}. The FPU rounds FP32 operands to about 11 significant bits, so the tolerance is deliberately
wider than FP32 epsilon; `ttl.exp` and other SFPU functions keep FP32 precision.

## Test cases

{cases}

Inputs are generated from a fixed seed, so every submission sees the same data.

## Limits and quota

- One case at a time, **180 s wall time per case** including TT-Lang compilation, and 8 GB host memory.
- **This problem accepts at most {limit} submissions in total**, counted over all accounts (pretests included).
  The problem page shows how many are used. Test locally first; you cannot buy back a wasted submission.
- Runtime: tt-lang `1.1.10.dev20260921` (bundled ttnn, Blackhole p300c). `ttl.grid_size(dims=1)` and
  `ttl.node(dims=1)` are supported in this version. The starter file is attached as `starter.py`.

## Profile summary

Every accepted case returns a profile summary on the record page: the **host timing** of the operation call
(5 warmups, 50 samples: median, P95, jitter, after JIT compilation), a separate instrumented **device pass**
with per-RISC kernel cycles (5 warmups, 10 samples), and the **TT-Lang perf summary** of that pass:
kernel duration, cores used, DRAM bytes read/written, transfer sizes and effective bandwidth. Use the host
median as "execution time" in your report and say so; parallel stage cycles must not be added together.
"""

COMMON_ZH = """
## 评测方式

评测器导入你的文件，只调用名为 **`{entry}`** 的 operation（签名见上），不会调用其他内容。输入已经是
device 0 上的 **FP32、tile 布局、DRAM** 张量，输出张量 `out` 已预分配并清零。不要自己打开/关闭设备，不要读
stdin，也不要打印答案。只有 TT-Lang operation 在硬件上运行；文件里的主机代码只在导入时执行一次。

{harness}

正确性：每个逻辑输出元素需满足 `|got - want| <= {atol:g} + {rtol:g} × scale`，其中 scale 为 {scale}。
FPU 会把 FP32 操作数舍入到约 11 位有效数字，所以容差比 FP32 的 epsilon 宽；`ttl.exp` 等 SFPU 函数保持 FP32 精度。

## 测试用例

{cases}

输入由固定随机种子生成，所有提交看到相同数据。

## 限制与配额

- 串行评测，**每个用例 180 秒墙钟**（含 TT-Lang 编译），8 GB 主机内存。
- **本题总共最多接受 {limit} 次提交**（所有账号合计，含 pretest）。题目页面会显示已用次数，请先在本地测试。
- 运行环境：tt-lang `1.1.10.dev20260921`（自带 ttnn，Blackhole p300c），支持 `ttl.grid_size(dims=1)` 与
  `ttl.node(dims=1)`。起始代码见附件 `starter.py`。

## 性能剖析

每个通过的用例都会在记录页返回性能摘要：operation 调用的**主机计时**（JIT 后 5 次热身、50 次采样：中位数、
P95、抖动）、单独的插桩**设备 pass**（各 RISC 的 kernel cycles，5 次热身、10 次采样），以及该 pass 的
**TT-Lang perf summary**：kernel 时长、使用核数、DRAM 读写字节、传输大小与有效带宽。报告里请用主机中位数作为
"执行时间"并注明；并行 stage 的 cycles 不能相加。
"""

PROBLEMS = {
    "ttlang-vector-add": {
        "id": "TT3001", "exercise": "vector-add", "entry": "vector_add",
        "title": "TT-Lang · Vector addition", "title_zh": "TT-Lang · 向量加法",
        "signature": "@ttl.operation(grid=(1, 1))\ndef vector_add(a, b, out):\n    ...",
        "task_en": "Implement **out = a + b** for FP32 vectors of length N (1 ≤ N ≤ 16,777,217) as a TT-Lang operation.",
        "task_zh": "用 TT-Lang 实现 FP32 向量加法 **out = a + b**，N 介于 1 与 16,777,217 之间。",
        "harness_en": ("Padding: the judge pads `a` and `b` with zeros to `P = 1024 * ceil(N / 1024)` values and uploads them as "
                       "`(P / 32, 32)` tiles; `out` has the same shape. Only the first N values of `out` (row-major) are checked."),
        "harness_zh": ("填充规则：评测器把 `a`、`b` 用零补到 `P = 1024 × ceil(N / 1024)` 个元素，并以 `(P / 32, 32)` 的 tile 形状上传；"
                       "`out` 形状相同。只检查 `out` 按行优先展开后的前 N 个值。"),
        "cases": [{"N": n} for n in VECTOR_LENGTHS],
        "case_header": ("| # | N | padded tiles |\n|---|---|---|\n", lambda i, c: f"| {i} | {c['N']:,} | {1024 * -(-c['N'] // 1024) // 1024} |"),
    },
    "ttlang-ragged-sum": {
        "id": "TT3002", "exercise": "ragged-sum", "entry": "ragged_sum",
        "title": "TT-Lang · Ragged row sum", "title_zh": "TT-Lang · 不等长行求和",
        "signature": "@ttl.operation(grid=(1, 1))\ndef ragged_sum(x, valid, out):\n    ...",
        "task_en": ("For `x` of shape `[R, L]` and `lengths[r]` in `[0, L]`, compute `out[r] = sum(x[r, :lengths[r]])`; "
                    "empty rows give 0."),
        "task_zh": "对形状 `[R, L]` 的 `x` 和 `lengths[r] ∈ [0, L]`，计算 `out[r] = sum(x[r, :lengths[r]])`；空行结果为 0。",
        "harness_en": ("Padding: `x` is zero-padded to `(R_pad, W_pad)` with both divisible by 32. `valid` has the same padded shape "
                       "and holds 1.0 on the first `lengths[r]` columns of row r and 0.0 elsewhere (padding included). "
                       "`out` has shape `(R_pad, 32)`; the judge reads column 0 of the first R rows. Wide rows "
                       "(W_pad up to 16,384 = 512 tiles) do not fit in L1 as one block: process rows in chunks and accumulate."),
        "harness_zh": ("填充规则：`x` 用零补到 `(R_pad, W_pad)`，两者均为 32 的倍数。`valid` 形状相同，第 r 行前 `lengths[r]` 列为 1.0，"
                       "其余（含填充）为 0.0。`out` 形状为 `(R_pad, 32)`，评测器读取前 R 行的第 0 列。宽行（W_pad 最大 16,384，"
                       "即 512 个 tile）无法整块放进 L1：请分块处理并累加。"),
        "cases": [{"R": r, "L": l} for r, l in RAGGED_CASES],
        "case_header": ("| # | R | L | padded (R_pad × W_pad) |\n|---|---|---|---|\n",
                        lambda i, c: f"| {i} | {c['R']:,} | {c['L']:,} | {32 * -(-c['R'] // 32):,} × {32 * -(-c['L'] // 32):,} |"),
    },
    "ttlang-ragged-softmax": {
        "id": "TT3003", "exercise": "ragged-softmax", "entry": "ragged_softmax",
        "title": "TT-Lang · Ragged softmax", "title_zh": "TT-Lang · 不等长行 softmax",
        "signature": "@ttl.operation(grid=(1, 1))\ndef ragged_softmax(x, valid, out):\n    ...",
        "task_en": ("For `x` of shape `[R, L]` with logits in `[-80, 80]` and `lengths[r]` in `[0, L]`, compute the softmax of each "
                    "valid prefix `x[r, :lengths[r]]`; entries outside the prefix and all entries of empty rows are 0."),
        "task_zh": ("对形状 `[R, L]`、取值在 `[-80, 80]` 的 logits `x` 和 `lengths[r] ∈ [0, L]`，对每行有效前缀 `x[r, :lengths[r]]` 做 softmax；"
                    "前缀之外以及空行的所有元素为 0。"),
        "harness_en": ("Padding: `x` and `valid` are padded exactly as in the ragged sum problem. `out` has the padded shape "
                       "`(R_pad, W_pad)`; the judge checks the top-left `R × L` block, so padded positions may hold any finite value. "
                       "Subtract the row maximum before exponentiating. Wide rows need more than one pass over the row. "
                       "Guard the empty-row denominator with `ttl.max(denominator, eps)` rather than by comparing it with 1: "
                       "the FPU rounds the row maximum, so a non-empty row's denominator can be slightly below 1."),
        "harness_zh": ("填充规则：`x` 和 `valid` 的填充方式与不等长行求和题相同。`out` 为填充后的形状 `(R_pad, W_pad)`，评测器只检查左上角 "
                       "`R × L` 区域，填充位置可以是任意有限值。指数运算前请先减去行最大值。宽行需要多趟遍历。"
                       "空行的分母请用 `ttl.max(denominator, eps)` 保护，不要和 1 比较：FPU 会舍入行最大值，非空行的分母可能略小于 1。"),
        "cases": [{"R": r, "L": l} for r, l in RAGGED_CASES],
        "case_header": ("| # | R | L | padded (R_pad × W_pad) |\n|---|---|---|---|\n",
                        lambda i, c: f"| {i} | {c['R']:,} | {c['L']:,} | {32 * -(-c['R'] // 32):,} × {32 * -(-c['L'] // 32):,} |"),
    },
}


def statement(spec, language):
    tol = TOLERANCES[spec["exercise"]]
    header, row = spec["case_header"]
    cases = header + "\n".join(row(i, c) for i, c in enumerate(spec["cases"], 1))
    common = (COMMON_EN if language == "en" else COMMON_ZH).format(
        entry=spec["entry"], harness=spec["harness_" + language], atol=tol["atol"], rtol=tol["rtol"],
        scale=tol["scale"] if language == "en" else {"vector-add": "每个元素的 |a| + |b|", "ragged-sum": "该行有效前缀内 |x| 之和",
                                                      "ragged-softmax": "该元素参考值的绝对值"}[spec["exercise"]],
        cases=cases, limit=SUBMISSION_LIMIT)
    head = (f"# {spec['title']}\n\n{spec['task_en']}\n\nSubmit one Python file that defines:\n\n```python\nimport ttl\n\n{spec['signature']}\n```\n"
            if language == "en" else
            f"# {spec['title_zh']}\n\n{spec['task_zh']}\n\n提交一个 Python 文件，定义：\n\n```python\nimport ttl\n\n{spec['signature']}\n```\n")
    return head + common


def main():
    for name, spec in PROBLEMS.items():
        folder = ROOT / "problems" / name
        folder.mkdir(parents=True, exist_ok=True)
        tol = TOLERANCES[spec["exercise"]]
        tests = []
        for index, case in enumerate(spec["cases"], 1):
            payload = {"exercise": spec["exercise"], "entry": spec["entry"], "seed": SEED, "case_id": index,
                       **case, "tolerance": {"atol": tol["atol"], "rtol": tol["rtol"]}}
            tests.append({"input": json.dumps(payload) + "\n", "output": VERDICT})
        existing = folder / "metadata.json"
        metadata = json.loads(existing.read_text()) if existing.exists() else {}
        metadata.update({
            "id": spec["id"], "title": spec["title"],
            "titles": {"zh": spec["title_zh"], "en": spec["title"]},
            "content": json.dumps({"zh": statement(spec, "zh"), "en": statement(spec, "en")}, ensure_ascii=False),
            "language": "ttlang", "mode": "ttlang", "time_ms": TIME_MS,
            "submission_limit": SUBMISSION_LIMIT,
            # The wrapper applies the tolerances; the worker only compares the verdict marker exactly.
            "atol": 0, "rtol": 0, "tolerance": {"atol": tol["atol"], "rtol": tol["rtol"], "scale": tol["scale"]},
            "tests": tests,
        })
        (folder / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
        entry = spec["entry"]
        (folder / "compile-error.py").write_text(f"import ttl\n\n@ttl.operation(grid=(1, 1))\ndef {entry}(*args)\n    pass\n")
        (folder / "runtime-error.py").write_text(f"import ttl\n\n\ndef {entry}(*args):\n    raise RuntimeError('Intentional test runtime failure')\n")
        print(f"wrote {folder.relative_to(ROOT)} ({len(tests)} cases)")


if __name__ == "__main__":
    main()
