# Tenstorrent 题源与下一批候选题

研究日期：2026-10-02 UTC。本文是题源调查与实施建议；下面 15 项尚未作为新题发布，也没有在本轮完成设备基线实测。对应可读 JSON 为 [problem-candidates.json](problem-candidates.json)。

## 结论

最有用的官方题源已经在当前 SDK 中：Metalium 编程例程提供 DRAM 搬运、SFPU、单核到多核矩阵乘法；TTNN 单元测试提供形状、padding、误差与模型算子的案例；模型代码和 tt-train 可为后续子图与反向算子提供来源。官方例程入口见 [Metalium Programming Examples](https://docs.tenstorrent.com/tt-metal/latest/tt-metalium/tt_metal/examples/index.html)，TTNN 教程见 [Tutorials](https://docs.tenstorrent.com/tt-metal/latest/ttnn/ttnn/tutorials.html)。

建议先补齐二元融合、不同形状 GEMM、稳定 softmax、reduction、normalization 五类，再做 Metalium 数据搬运与多 tile 题，最后加入 attention、MLP 子图和 backward。这是本项目的实施排序，不是官方发布的 benchmark。

## 当前基线与证据边界

- 硬件：2 张 p300c、4 颗 Blackhole ASIC；当前正式 worker 一个任务独占整组拓扑，实际使用 device 0。
- SDK：同一 `de546d3b146758714d900f11b218c8f9c805f410`，运行系列 v0.79.0，源码 `git describe` 显示 v0.79.0-rc2；容器 SFPI 7.78.0，驱动 2.10.0、固件 19.13.1。
- 已有 TT1001：BF16 TILE 加法，32×32、32×64、64×32；TT1002：三个小 GEMM，M/K/N 都是 32 或 64。
- 已有 TT2001：两个 uint32 值的 RISC-V 加法；TT2002：两个常数填充的 32×32 BF16 tile 加法。它们验证了提交与设备执行流程，尚不覆盖真实多 tile 数据与多核划分。
- 当前 Python runner 接受两个 BF16 TILE 输入 `solve(a,b)`，返回一个设备张量。它并未限制题目的数学运算只能是 add/matmul，因此同接口的融合题可复用；多输入、不同 dtype/layout、标量配置、多输出需要明确扩展契约。
- 当前 Metalium host 只支持两种固定单 kernel 题；一般例程不能仅靠上传新 manifest 接入。需要新增受控 host 模板或多文件提交协议。

候选 JSON 的 31 条源码路径均已检查：文件存在于固定 commit、工作区文件与该 Git 对象相同，并记录 SHA-256。网页文档是 `latest`，GitHub `main` 也会变化；具体出题必须使用 JSON 中的 commit 链接与文件哈希。本文的“源码存在”不表示候选题已经在本机 p300c 上通过。

## 15 个候选题

形状、精度和基线列均为待实施方案，必须先在固定 SDK、本机设备上校准。P0 可以从现有接口开始，P1 需要扩展输入或可信 host，P2 适合下一阶段。

| ID / 优先级 | 题目与覆盖内容 | 官方题源 | 接入要求 |
| --- | --- | --- | --- |
| C01 / P0 | Residual add + ReLU；负值、零、近零、融合与额外搬运 | [binary](https://github.com/tenstorrent/tt-metal/blob/de546d3b146758714d900f11b218c8f9c805f410/tests/ttnn/unit_tests/operations/eltwise/test_binary_category1_bfloat16.py)、[activation](https://github.com/tenstorrent/tt-metal/blob/de546d3b146758714d900f11b218c8f9c805f410/tests/ttnn/unit_tests/operations/eltwise/test_unary_activation.py) | 复用 `solve(a,b)`；对比 add 后 ReLU 与经核验的融合实现。先控制输出大小。 |
| C02 / P0 | GEMM 形状族；方阵、瘦长矩阵、不同 K、抵消、精度和配置 | [matmul tests](https://github.com/tenstorrent/tt-metal/blob/de546d3b146758714d900f11b218c8f9c805f410/tests/ttnn/unit_tests/operations/matmul/test_matmul.py)、[GEMM 报告](https://github.com/tenstorrent/tt-metal/blob/de546d3b146758714d900f11b218c8f9c805f410/tech_reports/GEMM_FLOPS/GEMM_FLOPS.md) | 两输入接口可用；大矩阵需解决完整 JSON 输出与 256 KiB stdout 限额，不能只扩 shape。 |
| C03 / P1 | Transpose 与布局转换；轴顺序、逻辑形状、padding | [permute tests](https://github.com/tenstorrent/tt-metal/blob/de546d3b146758714d900f11b218c8f9c805f410/tests/ttnn/unit_tests/operations/data_movement/test_permute.py) | 建立 unary 契约；BF16 精确比较逻辑值，布局不同的赛道分开。 |
| C04 / P1 | Row sum / mean；长行、负值抵消、非 32 倍数宽度 | [reduction tests](https://github.com/tenstorrent/tt-metal/blob/de546d3b146758714d900f11b218c8f9c805f410/tests/ttnn/unit_tests/operations/reduce/test_reduction.py) | unary、固定 dim 与 keepdim；参考答案使用量化后的输入，明确 padding 不参与运算。 |
| C05 / P1 | 稳定 row softmax；长行、相同最大值、极端 logits、padding | [softmax tests](https://github.com/tenstorrent/tt-metal/blob/de546d3b146758714d900f11b218c8f9c805f410/tests/ttnn/unit_tests/operations/fused/test_softmax.py)、[API](https://docs.tenstorrent.com/tt-metal/latest/ttnn/ttnn/api/ttnn.softmax.html) | unary；从 4D `[1,1,32,64]` 开始；固定 `numeric_stable=True` 基线。 |
| C06 / P1 | RMSNorm；gamma、epsilon、零行、低幅度、残差融合 | [RMSNorm tests](https://github.com/tenstorrent/tt-metal/blob/de546d3b146758714d900f11b218c8f9c805f410/tests/ttnn/unit_tests/operations/fused/test_rms_norm.py)、[API](https://docs.tenstorrent.com/tt-metal/latest/ttnn/ttnn/api/ttnn.rms_norm.html) | x/gamma 两个 TILE 输入可能复用；完整 residual 与 ROW_MAJOR 权重需要多输入及布局配置。 |
| C07 / P1 | LayerNorm；方差定义、affine、constant rows、Welford 精度 | [LayerNorm tests](https://github.com/tenstorrent/tt-metal/blob/de546d3b146758714d900f11b218c8f9c805f410/tests/ttnn/unit_tests/operations/fused/test_layer_norm.py)、[API](https://docs.tenstorrent.com/tt-metal/latest/ttnn/ttnn/api/ttnn.layer_norm.html) | x/gamma/beta 三输入；固定 biased variance 与 epsilon；不同数值路径分开记录。 |
| C08 / P2 | SDPA prefill；Q/K/V、causal mask、chunk、序列长度 | [SDPA tests](https://github.com/tenstorrent/tt-metal/blob/de546d3b146758714d900f11b218c8f9c805f410/tests/ttnn/unit_tests/operations/sdpa/test_sdpa_prefill.py) | 至少三张量、mask/scale 配置；先 BF16、S=128，再做 S=512。 |
| C09 / P2 | SwiGLU / gated MLP 子图；GEMM、activation、fusion | [模型 MLP](https://github.com/tenstorrent/tt-metal/blob/de546d3b146758714d900f11b218c8f9c805f410/models/tt_transformers/tt/mlp.py) | x 和三份权重；抽出单芯片子图，使用合成权重；不要依赖外部 checkpoint。 |
| C10 / P1 | DRAM→L1→DRAM copy；barrier、页寻址、带宽 | [loopback source](https://github.com/tenstorrent/tt-metal/blob/de546d3b146758714d900f11b218c8f9c805f410/tt_metal/programming_examples/loopback/loopback.cpp)、[教程](https://docs.tenstorrent.com/tt-metal/latest/tt-metalium/tt_metal/examples/dram_loopback.html) | 新可信 host：N tile buffers、runtime args；逐字节检查。 |
| C11 / P1 | 多 tile / 多核 BF16 vector add；非恒定输入、尾部分工 | [multicore vector add](https://github.com/tenstorrent/tt-metal/blob/de546d3b146758714d900f11b218c8f9c805f410/tt_metal/programming_examples/vecadd_multi_core/vecadd_multi_core.cpp) | 增加真实张量输入与 reader/writer；当前常数 tile 题无法覆盖。 |
| C12 / P1 | SFPU exp；数学近似、tile register、流水与 CB | [SFPU source](https://github.com/tenstorrent/tt-metal/blob/de546d3b146758714d900f11b218c8f9c805f410/tt_metal/programming_examples/eltwise_sfpu/eltwise_sfpu.cpp)、[教程](https://docs.tenstorrent.com/tt-metal/latest/tt-metalium/tt_metal/examples/eltwise_sfpu.html) | unary host、tile count 参数；先输入范围 [-5,5]，明确近似模式。 |
| C13 / P1 | SFPI smoothstep；自定义向量数学、边界、谓词 | [smoothstep source](https://github.com/tenstorrent/tt-metal/blob/de546d3b146758714d900f11b218c8f9c805f410/tt_metal/programming_examples/custom_sfpi_smoothstep/custom_sfpi_smoothstep.cpp)、[教程](https://docs.tenstorrent.com/tt-metal/latest/tt-metalium/tt_metal/examples/custom_sfpi_smoothstep.html) | 新 host 和 edge 参数；沿用容器固定 SFPI，先测试端点和范围。 |
| C14 / P2 | Metalium GEMM；single core→multicore→reuse→multicast | [单核](https://github.com/tenstorrent/tt-metal/blob/de546d3b146758714d900f11b218c8f9c805f410/tt_metal/programming_examples/matmul/matmul_single_core/matmul_single_core.cpp)、[多核优化教程](https://docs.tenstorrent.com/tt-metal/latest/tt-metalium/tt_metal/examples/matmul_multi_core_optimized.html) | 多文件 reader/compute/writer 或受控模板槽；为四种策略保留相同任务语义。 |
| C15 / P2 | Softmax backward / training op；梯度、前后向一致性 | [backward tests](https://github.com/tenstorrent/tt-metal/blob/de546d3b146758714d900f11b218c8f9c805f410/tt-train/tests/ops/softmax_backward_op_test.cpp)、[tt-train](https://github.com/tenstorrent/tt-metal/blob/main/tt-train/README.md) | 两张量 TTNN composition 可先探索；native tt-train 需另建镜像/host，现有 C++ kernel-only runner 不支持。 |

## Blackhole 与当前 SDK 的限制

1. **用实际设备 grid。** 官方 GEMM 性能报告含 P150 / 13×10 的结果；本机 p300c 不能直接使用这些结果作为自己的性能基线。具体 available grid、harvesting、dispatch 预留核心应从运行时查询。[官方 GEMM 报告](https://github.com/tenstorrent/tt-metal/blob/main/tech_reports/GEMM_FLOPS/GEMM_FLOPS.md)
2. **NoC 地址与同步需要适配。** 使用固定源码中的 TensorAccessor 与 barrier，不硬编码教程演示用的 Wormhole DRAM bank 数；Blackhole 的对齐、缓存、NoC 有自身约束。[官方 Blackhole 编程说明](https://github.com/tenstorrent/tt-metal/blob/main/tech_reports/Blackhole/BlackholeBringUpProgrammingGuide.md)
3. **测试源码不能整包搬来。** 固定 SDPA helper 的若干路径显式建立 `WormholeComputeKernelConfig`；需要按 `device.arch()` 选择配置并在本机运行。固定 permute 与 backward 测试也含架构/SKU 特定分支。此类标记应保留为题目来源说明，不能当成本机通过的证明。
4. **大数据需要不同传输方式。** 当前逐元素 JSON 输出和输出上限不适合所有大形状；要实现受控二进制结果读取或结果 artifact，再扩展性能规模。检查输出应由可信 host 完成，不能只让用户报告 checksum。
5. **官方精度检查是题源线索。** softmax、norm 代码有不同形状、dtype、路径的阈值。新题需要从独立 CPU reference、定量误差和本机基线校准出自己的冻结阈值；不能沿用全站统一误差，也不能仅使用相关系数 PCC 判正确。

## 版权与归属

固定 tt-metal 的根 LICENSE、NOTICE，以及被选中的 C++/Python 源文件 SPDX 头均已检查；tt-train 在此 commit 有自己的 Apache-2.0 LICENSE。[tt-metal LICENSE](https://github.com/tenstorrent/tt-metal/blob/main/LICENSE)、[固定 NOTICE](https://github.com/tenstorrent/tt-metal/blob/de546d3b146758714d900f11b218c8f9c805f410/NOTICE)、[tt-train LICENSE](https://github.com/tenstorrent/tt-metal/blob/de546d3b146758714d900f11b218c8f9c805f410/tt-train/LICENSE)

改编例程为题目或参考实现时，保留适用的版权/SPDX、Apache LICENSE、NOTICE 归属，并注明本项目修改。题面写清“改编自 Tenstorrent 官方例程/测试，固定 commit”，不要暗示官方认证。此许可证判断针对所选源文件；第三方依赖、预训练权重、数据集另有条款。本轮只建立链接与目录，没有导入模型权重、数据集或额外第三方代码。

## 下一步具体怎么做

### 第一批：冻结少量题目与基线

先实现 C01、C02、C04、C05、C06 的小规模版本。C01/C02 从现有二输入接口出发；C04/C05/C06 同时推动通用张量输入协议，解决 unary、多参数、dtype/layout 和输出大小问题。每题产物：纯英文题面与示例、语义声明、固定数据生成器与 seed、CPU reference、至少一个可运行设备基线、数值容差、来源归属、通过/错误/超时样例。

建议覆盖 tile 边界（31/32/33、63/64/65）、aligned 与 unaligned、短行与长行、方阵与矩形、常数/零/负数/抵消、不同数据复用程度。某个形状不受当前 API 支持时，明确列出排除规则；不要发布在本机没有跑过的测试组合。

### 第二批：使优化空间真实可用

引入 C10/C11/C12 的可信 host 模板，给予多 tile 与多核优化空间；再加入 C07/C08/C09。TTNN 允许调用现有算子，可作为 API/config/composition 赛道；Metalium 需要允许 reader/compute/writer 或可信模板槽，才能研究底层 kernel 优化。不同赛道分别记录，不能把 SDK 已有算子调用与手写 kernel 的开发难度混作一个任务。

### 研究与计时记录

- 为题目记录语义版本、source commit/hash、shape/dtype/layout、fidelity、容差、数据 seed/hash、baseline commit/hash、计时范围与协议版本。
- 正确性数据集与性能 shape 集要都冻结；保留完整元素检查、异常值与失败日志，失败原因先标为候选，人工和对照实验核验后再形成分类。
- host 同步调用延迟与设备 profiler 周期保留为两项；trace replay 另设协议，避免把减少主机 dispatch 开销后的结果混入普通调用。
- 先提供相同机器/协议下的可信基线速度比较；建立 SOL/roofline 指标需要额外推导 FLOP/数据流、使用真实可用核心和频率、独立校准，不能将公开 P150 峰值或最慢核心中位数 直接解释为本机硬件效率。
- 对源码与提交轨迹的后续研究使用，准备参与者可读的保存/公开说明；管理员创建账号时不会自动获得公开发布别人源码的授权。

候选进入正式题库的门槛：固定镜像上通过全部正确性样例，设备与主机计时范围清晰，跨新进程重复结果可解释，基线稳定性和原始记录完整。数量可以逐步增加；当前调查不把 15 个候选宣称为 15 道已经可用的 benchmark 题。
