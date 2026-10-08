// SPDX-License-Identifier: Apache-2.0
// Bounded device-side waiting demonstrates overlap; this is not a throughput benchmark.
#include "ckernel.h"

void kernel_main() {
    const uint32_t destination = get_arg_val<uint32_t>(0);
    const uint32_t scratch = get_arg_val<uint32_t>(1);
    const uint32_t tag = get_arg_val<uint32_t>(2);
    const uint32_t a = get_arg_val<uint32_t>(3);
    const uint32_t b = get_arg_val<uint32_t>(4);
    const uint32_t cycles = get_arg_val<uint32_t>(5);
    volatile tt_reg_ptr uint32_t* clock =
        reinterpret_cast<volatile tt_reg_ptr uint32_t*>(RISCV_DEBUG_REG_WALL_CLOCK_L);
    const uint32_t start = clock[0];
    while (static_cast<uint32_t>(clock[0] - start) < cycles) {}
    const uint32_t end = clock[0];
    uint32_t* output = reinterpret_cast<uint32_t*>(scratch);
    output[0] = tag;
    output[1] = a + b;
    output[2] = start;
    output[3] = end;
    output[4] = cycles;
    output[5] = 0x54544F4A;
    output[6] = 0;
    output[7] = 1;
    (void)ckernel::load_blocking(reinterpret_cast<volatile tt_l1_ptr uint32_t*>(scratch + 28));
    InterleavedAddrGen<true> dst = {.bank_base_address = destination, .page_size = 32};
    noc_async_write(scratch, dst.get_noc_addr(0), 32);
    noc_async_write_barrier();
}
