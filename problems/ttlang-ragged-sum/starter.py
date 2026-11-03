"""CS540 · TT-Lang ragged row sum starter.

The judge calls ragged_sum(x, valid, out) with FP32 tile-layout DRAM tensors:
x and valid are (R_pad, W_pad) with both divisible by 32; valid is 1.0 on the valid prefix of
each row and 0.0 elsewhere; out is (R_pad, 32) and the judge reads column 0 of the first R rows.

This starter handles one row group of 32 rows at a time and loads the full padded width as one
block, which only fits in L1 for narrow rows (W_pad <= 256). Wide cases need chunking.
"""
import ttl


@ttl.operation(grid=(1, 1))
def ragged_sum(x, valid, out):
    assert x.shape[0] % 32 == 0 and x.shape[1] % 32 == 0
    assert 0 < x.shape[1] <= 256, "TODO: split wide rows into chunks and accumulate"
    row_groups = x.shape[0] // 32
    width_tiles = x.shape[1] // 32
    x_dfb = ttl.make_dataflow_buffer_like(x, shape=(1, width_tiles), block_count=2)
    valid_dfb = ttl.make_dataflow_buffer_like(valid, shape=(1, width_tiles), block_count=2)
    out_dfb = ttl.make_dataflow_buffer_like(out, shape=(1, 1), block_count=2)

    @ttl.compute()
    def compute():
        for _ in range(row_groups):
            with x_dfb.wait() as x_blk, valid_dfb.wait() as valid_blk, out_dfb.reserve() as out_blk:
                out_blk.store(ttl.math.reduce_sum(x_blk * valid_blk, dims=[1], shape=(1, 1)))

    @ttl.datamovement()
    def reader():
        for row in range(row_groups):
            with x_dfb.reserve() as x_blk, valid_dfb.reserve() as valid_blk:
                tx_x = ttl.copy(x[row:row + 1, 0:width_tiles], x_blk)
                tx_valid = ttl.copy(valid[row:row + 1, 0:width_tiles], valid_blk)
                tx_x.wait()
                tx_valid.wait()

    @ttl.datamovement()
    def writer():
        for row in range(row_groups):
            with out_dfb.wait() as out_blk:
                ttl.copy(out_blk, out[row:row + 1, 0:1]).wait()
