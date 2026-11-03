"""CS540 · TT-Lang ragged row sum.

x and valid are FP32 tile-layout DRAM tensors of shape (R_pad, W_pad); valid is 1.0 on the first
lengths[r] columns of row r and 0.0 elsewhere. out is (R_pad, 32): write the sum of the valid
prefix of row r to out[r, 0]. Rows can be up to 16,384 values (512 tiles) wide.
See the problem statement for the shapes of the ten testcases and the tolerance.
"""
import ttl


@ttl.operation(grid=(1, 1))
def ragged_sum(x, valid, out):
    # TODO: create the dataflow buffers, then write the reader, compute and writer kernels.
    ...
