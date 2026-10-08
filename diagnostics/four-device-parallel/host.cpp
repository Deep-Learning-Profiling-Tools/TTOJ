// SPDX-License-Identifier: Apache-2.0
#include <chrono>
#include <cstdint>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <vector>
#include <nlohmann/json.hpp>
#include <tt-metalium/host_api.hpp>
#include <tt-metalium/distributed.hpp>

using namespace tt;
using namespace tt::tt_metal;
using Clock = std::chrono::steady_clock;

struct Job {
    int id;
    std::shared_ptr<distributed::MeshDevice> device;
    distributed::MeshWorkload workload;
    std::shared_ptr<distributed::MeshBuffer> output;
    std::shared_ptr<distributed::MeshBuffer> scratch;
    uint32_t cycles;
    Job(int device_id, std::shared_ptr<distributed::MeshDevice> mesh) : id(device_id), device(std::move(mesh)) {
        distributed::ReplicatedBufferConfig size{.size = 32};
        distributed::DeviceLocalBufferConfig dram{.page_size = 32, .buffer_type = BufferType::DRAM};
        distributed::DeviceLocalBufferConfig l1{.page_size = 32, .buffer_type = BufferType::L1};
        output = distributed::MeshBuffer::create(size, dram, device.get());
        scratch = distributed::MeshBuffer::create(size, l1, device.get());
        auto program = CreateProgram();
        const CoreCoord core{0, 0};
        const auto kernel = CreateKernel(program, "/workspace/kernel.cpp", core,
            DataMovementConfig{.processor = DataMovementProcessor::RISCV_0, .noc = NOC::RISCV_0_default});
        const int mhz = device->get_clock_rate_mhz();
        if (mhz <= 0 || mhz > 3000) throw std::runtime_error("Invalid device clock");
        cycles = static_cast<uint32_t>(mhz) * 250000;
        SetRuntimeArgs(program, kernel, core, {static_cast<uint32_t>(output->address()),
            static_cast<uint32_t>(scratch->address()), static_cast<uint32_t>(0x12340000 + id),
            static_cast<uint32_t>(100 + id), static_cast<uint32_t>(200 + 3 * id), cycles});
        workload.add_program(distributed::MeshCoordinateRange(device->shape()), std::move(program));
    }
    void enqueue() { distributed::EnqueueMeshWorkload(device->mesh_command_queue(), workload, false); }
    void finish() { distributed::Finish(device->mesh_command_queue()); }
    nlohmann::json verify() {
        std::vector<uint32_t> result;
        distributed::EnqueueReadMeshBuffer(device->mesh_command_queue(), result, output, true);
        if (result.size() != 8 || result[0] != static_cast<uint32_t>(0x12340000 + id) ||
            result[1] != static_cast<uint32_t>(300 + 4 * id) || result[4] != cycles ||
            result[5] != 0x54544F4A || result[7] != 1 || static_cast<uint32_t>(result[3] - result[2]) < cycles)
            throw std::runtime_error("Device output verification failed");
        return {{"device_id", id}, {"tag", result[0]}, {"sum", result[1]},
            {"duration_cycles", static_cast<uint32_t>(result[3] - result[2])}, {"target_cycles", cycles}};
    }
};

int main() {
    try {
        auto devices = distributed::MeshDevice::create_unit_meshes({0, 1, 2, 3});
        std::vector<std::unique_ptr<Job>> jobs;
        for (auto& [id, device] : devices) jobs.push_back(std::make_unique<Job>(id, device));
        for (auto& job : jobs) { job->enqueue(); job->finish(); job->verify(); }
        nlohmann::json rounds = nlohmann::json::array();
        for (int round = 0; round < 3; ++round) {
            const auto start_serial = Clock::now();
            for (auto& job : jobs) { job->enqueue(); job->finish(); }
            const auto end_serial = Clock::now();
            for (auto& job : jobs) job->verify();
            const auto start_parallel = Clock::now();
            for (auto& job : jobs) job->enqueue();
            for (auto& job : jobs) job->finish();
            const auto end_parallel = Clock::now();
            nlohmann::json outputs = nlohmann::json::array();
            for (auto& job : jobs) outputs.push_back(job->verify());
            const double serial_ms = std::chrono::duration<double, std::milli>(end_serial - start_serial).count();
            const double parallel_ms = std::chrono::duration<double, std::milli>(end_parallel - start_parallel).count();
            rounds.push_back({{"serial_ms", serial_ms}, {"parallel_ms", parallel_ms},
                {"overlap_ratio", serial_ms / parallel_ms}, {"outputs", outputs}});
            if (parallel_ms >= serial_ms * 0.6) throw std::runtime_error("Four-device overlap was not demonstrated");
        }
        jobs.clear();
        for (auto& [id, device] : devices) device->close();
        std::cout << "TT_OJ_PARALLEL_RESULT:" << nlohmann::json({{"device_ids", {0, 1, 2, 3}},
            {"process_count", 1}, {"timing_scope", "cached_enqueue_to_completion_no_profiler"},
            {"kernel", "bounded_device_side_wait_then_add"}, {"nominal_wait_ms_per_device", 250},
            {"not_a_real_workload_speedup_guarantee", true}, {"rounds", rounds}}).dump() << std::endl;
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << std::endl;
        return 1;
    }
}
