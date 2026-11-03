"""CS540 · TT-Lang vector addition.

a, b and out are FP32 tile-layout DRAM tensors of shape (P / 32, 32), P = 1024 * ceil(N / 1024).
Compute out = a + b; only the first N values of out are checked.
See the problem statement for the shapes of the ten testcases and the tolerance.
"""
import ttl


@ttl.operation(grid=(1, 1))
def vector_add(a, b, out):
    # TODO: create the dataflow buffers, then write the reader, compute and writer kernels.
    ...
