// SPDX-License-Identifier: Apache-2.0
// Adapted from Tenstorrent add_2_integers_in_compute (2024 Tenstorrent USA, Inc.).
#include <cmath>
#include <cstdint>
#include <exception>
#include <iomanip>
#include <iostream>
#include <memory>
#include <vector>
#include <tt-metalium/host_api.hpp>
#include <tt-metalium/device.hpp>
#include <tt-metalium/bfloat16.hpp>
#include <tt-metalium/constants.hpp>
#include <tt-metalium/distributed.hpp>
#include "profiling.hpp"

using namespace tt;
using namespace tt::tt_metal;

int main() {
    if (tt_oj::prepare_environment()) return 0;
    float input0 = 0, input1 = 0;
    if (!(std::cin >> input0 >> input1) || !std::isfinite(input0) || !std::isfinite(input1)) {
        std::cerr << "TT_OJ_INPUT_ERROR: expected two finite numbers\n";
        return 2;
    }
    try {
        auto device = distributed::MeshDevice::create_unit_mesh(0);
        auto& queue = device->mesh_command_queue();
        distributed::MeshWorkload workload;
        distributed::MeshCoordinateRange range(device->shape());
        Program program = CreateProgram();
        constexpr CoreCoord core = {0, 0};
        constexpr uint32_t elements = 32 * 32;
        constexpr uint32_t bytes = sizeof(bfloat16) * elements;
        distributed::DeviceLocalBufferConfig dram{.page_size = bytes, .buffer_type = BufferType::DRAM};
        distributed::ReplicatedBufferConfig config{.size = bytes};
        auto src0 = distributed::MeshBuffer::create(config, dram, device.get());
        auto src1 = distributed::MeshBuffer::create(config, dram, device.get());
        auto dst = distributed::MeshBuffer::create(config, dram, device.get());
        auto cb_config = [&](CBIndex index) {
            return CircularBufferConfig(bytes, {{index, DataFormat::Float16_b}}).set_page_size(index, bytes);
        };
        CreateCircularBuffer(program, core, cb_config(CBIndex::c_0));
        CreateCircularBuffer(program, core, cb_config(CBIndex::c_1));
        CreateCircularBuffer(program, core, cb_config(CBIndex::c_16));

        const auto reader = CreateKernel(
            program, "/opt/tt-oj/kernels/reader_binary_1_tile.cpp",
            core, DataMovementConfig{.processor = DataMovementProcessor::RISCV_1, .noc = NOC::RISCV_1_default});
        const auto writer = CreateKernel(
            program, "/opt/tt-oj/kernels/writer_1_tile.cpp",
            core, DataMovementConfig{.processor = DataMovementProcessor::RISCV_0, .noc = NOC::RISCV_0_default});
        const auto compute = CreateKernel(
            program, "/workspace/solution.cpp", core,
            ComputeConfig{.math_fidelity = MathFidelity::HiFi4, .fp32_dest_acc_en = false, .math_approx_mode = false});
        std::vector<bfloat16> a(elements, bfloat16(input0)), b(elements, bfloat16(input1));
        EnqueueWriteMeshBuffer(queue, src0, a, false);
        EnqueueWriteMeshBuffer(queue, src1, b, false);
        SetRuntimeArgs(program, reader, core, {
            static_cast<uint32_t>(src0->address()), static_cast<uint32_t>(src1->address())});
        SetRuntimeArgs(program, compute, core, {});
        SetRuntimeArgs(program, writer, core, {static_cast<uint32_t>(dst->address())});
        workload.add_program(range, std::move(program));
        distributed::EnqueueMeshWorkload(queue, workload, false);
        std::vector<bfloat16> result;
        distributed::EnqueueReadMeshBuffer(queue, result, dst, true);
        if (result.size() != elements) throw std::runtime_error("incorrect output tile size");
        const float expected = static_cast<float>(bfloat16(
            static_cast<float>(bfloat16(input0)) + static_cast<float>(bfloat16(input1))));
        auto all_correct = [&](const std::vector<bfloat16>& values) {
            for (auto value : values) if (static_cast<float>(value) != expected) return false;
            return true;
        };
        const bool initially_correct = all_correct(result);
        auto profile = tt_oj::measure(*device, queue, workload, device->get_clock_rate_mhz(), initially_correct);
        if ((tt_oj::enabled("TT_OJ_BENCHMARK") || tt_oj::enabled("TT_OJ_PROFILE_ONLY")) && initially_correct) {
            distributed::EnqueueReadMeshBuffer(queue, result, dst, true);
            if (result.size() != elements) throw std::runtime_error("incorrect final output tile size");
            profile["final_correct"] = all_correct(result);
            profile["correctness_verified"] = all_correct(result);
        }
        std::vector<float> actual;
        actual.reserve(elements);
        for (auto value : result) {
            const float number = static_cast<float>(value);
            if (!std::isfinite(number)) throw std::runtime_error("kernel produced nonfinite output");
            actual.push_back(number);
        }
        profile["device_clock_mhz_end"] = device->get_clock_rate_mhz();
        tt_oj::collect_device_profile(*device);
        device->close();
        tt_oj::finish_metadata(profile);
        if (tt_oj::enabled("TT_OJ_BENCHMARK") || tt_oj::enabled("TT_OJ_PROFILE_ONLY")) tt_oj::emit(profile);
        std::cout << "TT_OJ_RESULT:[" << std::setprecision(9);
        for (size_t i = 0; i < actual.size(); ++i) {
            if (i) std::cout << ',';
            std::cout << actual[i];
        }
        std::cout << "]\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "TT_OJ_RUNTIME_ERROR: " << error.what() << '\n';
        return 1;
    }
}
