// SPDX-License-Identifier: Apache-2.0
// Adapted from Tenstorrent add_2_integers_in_riscv (2025 Tenstorrent USA, Inc.).
#include <cstdint>
#include <exception>
#include <iostream>
#include <limits>
#include <memory>
#include <vector>
#include <tt-metalium/host_api.hpp>
#include <tt-metalium/device.hpp>
#include <tt-metalium/distributed.hpp>
#include "profiling.hpp"

using namespace tt;
using namespace tt::tt_metal;

int main() {
    if (tt_oj::prepare_environment()) return 0;
    uint64_t input0 = 0, input1 = 0;
    if (!(std::cin >> input0 >> input1) || input0 > UINT32_MAX || input1 > UINT32_MAX) {
        std::cerr << "TT_OJ_INPUT_ERROR: expected two uint32 values\n";
        return 2;
    }
    try {
        auto device = distributed::MeshDevice::create_unit_mesh(0);
        auto& queue = device->mesh_command_queue();
        distributed::MeshWorkload workload;
        distributed::MeshCoordinateRange range(device->shape());
        Program program = CreateProgram();
        constexpr CoreCoord core = {0, 0};
        constexpr uint32_t bytes = sizeof(uint32_t);
        distributed::DeviceLocalBufferConfig dram{.page_size = bytes, .buffer_type = BufferType::DRAM};
        distributed::DeviceLocalBufferConfig l1{.page_size = bytes, .buffer_type = BufferType::L1};
        distributed::ReplicatedBufferConfig config{.size = bytes};
        auto src0 = distributed::MeshBuffer::create(config, dram, device.get());
        auto src1 = distributed::MeshBuffer::create(config, dram, device.get());
        auto dst = distributed::MeshBuffer::create(config, dram, device.get());
        auto l1_src0 = distributed::MeshBuffer::create(config, l1, device.get());
        auto l1_src1 = distributed::MeshBuffer::create(config, l1, device.get());
        auto l1_dst = distributed::MeshBuffer::create(config, l1, device.get());
        std::vector<uint32_t> a{static_cast<uint32_t>(input0)}, b{static_cast<uint32_t>(input1)};
        EnqueueWriteMeshBuffer(queue, src0, a, false);
        EnqueueWriteMeshBuffer(queue, src1, b, false);

        const auto kernel = CreateKernel(
            program, "/workspace/solution.cpp", core,
            DataMovementConfig{.processor = DataMovementProcessor::RISCV_0, .noc = NOC::RISCV_0_default});
        SetRuntimeArgs(program, kernel, core, {
            static_cast<uint32_t>(src0->address()), static_cast<uint32_t>(src1->address()),
            static_cast<uint32_t>(dst->address()), static_cast<uint32_t>(l1_src0->address()),
            static_cast<uint32_t>(l1_src1->address()), static_cast<uint32_t>(l1_dst->address())});
        workload.add_program(range, std::move(program));
        distributed::EnqueueMeshWorkload(queue, workload, false);
        std::vector<uint32_t> result;
        distributed::EnqueueReadMeshBuffer(queue, result, dst, true);
        if (result.size() != 1) throw std::runtime_error("incorrect output buffer size");
        const uint32_t expected = static_cast<uint32_t>(input0 + input1);
        const uint32_t first_actual = result.front();
        auto profile = tt_oj::measure(*device, queue, workload, device->get_clock_rate_mhz(), first_actual == expected);
        if ((tt_oj::enabled("TT_OJ_BENCHMARK") || tt_oj::enabled("TT_OJ_PROFILE_ONLY")) && first_actual == expected) {
            distributed::EnqueueReadMeshBuffer(queue, result, dst, true);
            if (result.size() != 1) throw std::runtime_error("incorrect final output buffer size");
            profile["final_correct"] = result.front() == expected;
            profile["correctness_verified"] = result.front() == expected;
        }
        const uint32_t actual = first_actual == expected ? result.front() : first_actual;
        profile["device_clock_mhz_end"] = device->get_clock_rate_mhz();
        tt_oj::collect_device_profile(*device);
        device->close();
        tt_oj::finish_metadata(profile);
        if (tt_oj::enabled("TT_OJ_BENCHMARK") || tt_oj::enabled("TT_OJ_PROFILE_ONLY")) tt_oj::emit(profile);
        std::cout << "TT_OJ_RESULT:" << actual << '\n';
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "TT_OJ_RUNTIME_ERROR: " << error.what() << '\n';
        return 1;
    }
}
