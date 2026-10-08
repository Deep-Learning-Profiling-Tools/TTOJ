cmake_minimum_required(VERSION 3.22)
project(tt_oj_metalium LANGUAGES CXX)
option(TT_OJ_DEVICE_PROFILER_SUPPORTED "Linked Metalium library has Tracy-enabled device profiling" OFF)

find_package(TT-Metalium REQUIRED)
foreach(mode riscv compute)
    add_executable(judge-${mode} judge-${mode}.cpp)
    target_link_libraries(judge-${mode} PRIVATE TT::Metalium)
    target_compile_definitions(judge-${mode} PRIVATE
        TT_OJ_DEVICE_PROFILER_SUPPORTED=$<BOOL:${TT_OJ_DEVICE_PROFILER_SUPPORTED}>)
    target_link_options(judge-${mode} PRIVATE
        "-L/opt/tt-oj/mpi" "-Wl,--no-as-needed" "-l:libmpi.so.40"
        "-Wl,-rpath-link,/opt/tt-oj/mpi" "-Wl,-rpath,/opt/tt-oj/mpi")
    install(TARGETS judge-${mode} RUNTIME DESTINATION bin)
endforeach()
