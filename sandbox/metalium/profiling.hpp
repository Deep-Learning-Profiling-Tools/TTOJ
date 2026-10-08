// SPDX-License-Identifier: Apache-2.0
#pragma once

#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <set>
#include <sstream>
#include <string>
#include <time.h>
#include <vector>
#include <nlohmann/json.hpp>
#include <tt-metalium/host_api.hpp>
#include <tt-metalium/distributed.hpp>

#ifndef TT_OJ_DEVICE_PROFILER_SUPPORTED
#define TT_OJ_DEVICE_PROFILER_SUPPORTED 0
#endif

namespace tt_oj {
using Clock = std::chrono::steady_clock;
using Json = nlohmann::json;
static_assert(Clock::is_steady, "benchmark clock must be monotonic");

inline bool enabled(const char* variable) {
    const char* value = std::getenv(variable);
    return value && std::string(value) == "1";
}

inline Json clock_resolution_ns() {
    timespec resolution{};
    if (clock_getres(CLOCK_MONOTONIC, &resolution) != 0) return nullptr;
    return static_cast<int64_t>(resolution.tv_sec) * 1000000000LL + resolution.tv_nsec;
}

inline Json metadata() {
    return {
        {"schema_version", 1}, {"sdk_version", "TT-Metalium 0.79.0"},
        {"timing_scope", "host_submit_to_device_complete"}, {"clock", "steady_clock"},
        {"units", "ns"}, {"clock_resolution_ns", clock_resolution_ns()},
        {"clock_resolution_note", "Reported clock resolution is not measurement accuracy."},
        {"warmup", 0}, {"samples_ns", Json::array()}, {"device_clock_mhz", nullptr},
        {"measurement_start_ns", nullptr}, {"measurement_end_ns", nullptr},
        {"device_profiler", {
            {"available", TT_OJ_DEVICE_PROFILER_SUPPORTED != 0},
            {"enabled", false},
            {"reason", TT_OJ_DEVICE_PROFILER_SUPPORTED ? "disabled_for_host_benchmark" : "packaged_library_profiler_stubs"},
            {"csv_path", "/tmp/generated/profiler/.logs/profile_log_device.csv"}
        }}
    };
}

inline void emit(const Json& profile) {
    std::cout << "TT_OJ_PROFILE:" << profile.dump() << '\n';
}

// Returns true when the diagnostic request is handled without device access.
inline bool prepare_environment() {
    const bool profile_only = enabled("TT_OJ_PROFILE_ONLY");
    if (profile_only && !TT_OJ_DEVICE_PROFILER_SUPPORTED) {
        auto profile = metadata();
        profile["timing_scope"] = "device_profiler_unavailable";
        profile["device_profiler"]["details"] =
            "ReadMeshDeviceProfilerResults and InitDeviceProfiler are no-op stubs in the installed release. "
            "TT_METAL_DEVICE_PROFILER requires a Tracy-enabled Metalium build.";
        emit(profile);
        return true;
    }
    if (enabled("TT_OJ_BENCHMARK") || profile_only) {
        // Profiling, watcher and DPRINT share SRAM and affect kernel timing.
        unsetenv("TT_METAL_DPRINT_CORES");
        unsetenv("TT_METAL_WATCHER");
        unsetenv("TT_METAL_STREAMING_PROFILER");
        unsetenv("TT_METAL_DEVICE_PROFILER");
    }
    if (profile_only) {
        setenv("TT_METAL_DEVICE_PROFILER", "1", 1);
        setenv("TT_METAL_PROFILER_DIR", "/tmp/generated/profiler", 1);
        setenv("TT_METAL_PROFILER_DISABLE_PUSH_TO_TRACY", "1", 1);
        setenv("TT_METAL_PROFILER_CPP_POST_PROCESS", "0", 1);
        setenv("TT_METAL_PROFILER_MID_RUN_DUMP", "1", 1);
    }
    return false;
}

inline Json measure(
    tt::tt_metal::distributed::MeshDevice& device,
    tt::tt_metal::distributed::MeshCommandQueue& queue,
    tt::tt_metal::distributed::MeshWorkload& workload,
    int clock_mhz,
    bool initially_correct) {
    auto profile = metadata();
    profile["device_clock_mhz"] = clock_mhz > 0 ? Json(clock_mhz) : Json(nullptr);
    profile["first_correct"] = initially_correct;
    profile["final_correct"] = initially_correct;
    profile["correctness_verified"] = initially_correct;
    profile["instrumented"] = enabled("TT_OJ_PROFILE_ONLY");
    auto invoke = [&] {
        tt::tt_metal::distributed::EnqueueMeshWorkload(queue, workload, false);
        tt::tt_metal::distributed::Finish(queue);
    };
    if (enabled("TT_OJ_PROFILE_ONLY")) {
        profile["timing_scope"] = "device_kernel_cycles";
        profile["device_profiler"]["enabled"] = true;
        profile["device_profiler"]["reason"] = "warmed_instrumented_diagnostic_pass";
        const std::filesystem::path csv = "/tmp/generated/profiler/.logs/profile_log_device.csv";
        auto size = [&] {
            return std::filesystem::exists(csv) ? std::filesystem::file_size(csv) : uintmax_t{0};
        };
        auto flush = [&] { tt::tt_metal::ReadMeshDeviceProfilerResults(device); };
        Json capture = {
            {"selection", "csv_append_byte_ranges"}, {"warmup", 5}, {"sample_count", 10},
            {"sample_byte_ranges", Json::array()}, {"raw_csv_preserves_warmups", true},
            {"note", "Instrumented repeats are diagnostic; profiler flush occurs outside each kernel interval."}
        };
        flush();  // Keep the initial correctness/JIT invocation in the raw CSV.
        for (int i = 0; i < 5 && initially_correct; ++i) {
            invoke();
            flush();
        }
        capture["warmup_end_byte"] = size();
        profile["warmup"] = initially_correct ? 5 : 0;
        if (initially_correct) {
            profile["measurement_start_ns"] = std::chrono::duration_cast<std::chrono::nanoseconds>(
                Clock::now().time_since_epoch()).count();
            for (int sample = 1; sample <= 10; ++sample) {
                const auto begin = size();
                invoke();
                flush();
                const auto end = size();
                std::set<uint64_t> run_ids;
                std::ifstream input(csv);
                input.seekg(static_cast<std::streamoff>(begin));
                std::string line;
                while (input && input.tellg() < static_cast<std::streamoff>(end) && std::getline(input, line)) {
                    std::vector<std::string> columns;
                    std::istringstream row(line);
                    std::string item;
                    while (std::getline(row, item, ',')) columns.push_back(item);
                    if (columns.size() >= 12 && columns[10].ends_with("-KERNEL")) {
                        try { run_ids.insert(std::stoull(columns[7])); } catch (const std::exception&) {}
                    }
                }
                capture["sample_byte_ranges"].push_back({
                    {"sample", sample}, {"start_byte", begin}, {"end_byte", end},
                    {"run_host_ids", run_ids}, {"valid", end > begin}
                });
            }
            profile["measurement_end_ns"] = std::chrono::duration_cast<std::chrono::nanoseconds>(
                Clock::now().time_since_epoch()).count();
        }
        profile["device_capture"] = std::move(capture);
        return profile;
    }
    if (!enabled("TT_OJ_BENCHMARK") || !initially_correct) return profile;
    constexpr int warmup = 5;
    constexpr int samples = 50;
    std::vector<int64_t> durations;
    durations.reserve(samples);
    // The first correctness invocation has already compiled firmware/kernels,
    // uploaded inputs, run the workload and read back output before this call.
    tt::tt_metal::distributed::Finish(queue);
    for (int i = 0; i < warmup; ++i) invoke();
    for (int i = 0; i < samples; ++i) {
        const auto start = Clock::now();
        invoke();
        const auto end = Clock::now();
        if (i == 0) profile["measurement_start_ns"] =
            std::chrono::duration_cast<std::chrono::nanoseconds>(start.time_since_epoch()).count();
        if (i == samples - 1) profile["measurement_end_ns"] =
            std::chrono::duration_cast<std::chrono::nanoseconds>(end.time_since_epoch()).count();
        durations.push_back(std::chrono::duration_cast<std::chrono::nanoseconds>(end - start).count());
    }
    profile["warmup"] = warmup;
    profile["samples_ns"] = durations;
    profile["sampling"] = "Each sample submits one cached workload and waits for Finish; queue is idle at start.";
    profile["isolation_note"] = "Samples share one process and cached workload. CPU affinity and OS scheduling isolation are controlled by the worker.";
    profile["includes"] = {"host_enqueue", "dispatch", "device_workload", "completion_wait"};
    profile["excludes"] = {"container_startup", "device_initialization", "JIT", "input_upload", "output_download"};
    return profile;
}

inline void collect_device_profile(tt::tt_metal::distributed::MeshDevice& device) {
    if (enabled("TT_OJ_PROFILE_ONLY")) {
        tt::tt_metal::ReadMeshDeviceProfilerResults(device);
    }
}

inline void finish_metadata(Json& profile) {
    if (enabled("TT_OJ_PROFILE_ONLY")) {
        const bool present = std::filesystem::is_regular_file(
            "/tmp/generated/profiler/.logs/profile_log_device.csv");
        profile["device_profiler"]["csv_present"] = present;
        if (!present) profile["device_profiler"]["reason"] = "enabled_but_no_csv_exported";
    }
}
}  // namespace tt_oj
