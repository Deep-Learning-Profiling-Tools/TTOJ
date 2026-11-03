"""CS540 · TT-Lang ragged softmax.

x, valid and out are FP32 tile-layout DRAM tensors of shape (R_pad, W_pad); valid is 1.0 on the
first lengths[r] columns of row r and 0.0 elsewhere. Write the softmax of each valid prefix to out;
positions outside the prefix and empty rows must be 0.
See the problem statement for the shapes of the ten testcases and the tolerance.
"""
import ttl


@ttl.operation(grid=(1, 1))
def ragged_softmax(x, valid, out):
    # TODO: create the dataflow buffers, then write the reader, compute and writer kernels.
    ...
