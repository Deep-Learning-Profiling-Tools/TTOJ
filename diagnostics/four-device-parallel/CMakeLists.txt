cmake_minimum_required(VERSION 3.22)
project(tt_oj_four_device_parallel LANGUAGES CXX)
find_package(TT-Metalium REQUIRED)
add_executable(four-device-parallel host.cpp)
target_link_libraries(four-device-parallel PRIVATE TT::Metalium)
target_link_options(four-device-parallel PRIVATE
    "-L/opt/tt-oj/mpi" "-Wl,--no-as-needed" "-l:libmpi.so.40"
    "-Wl,-rpath-link,/opt/tt-oj/mpi" "-Wl,-rpath,/opt/tt-oj/mpi")
