"""Reference solution: single core, rows in groups of 32, width processed in chunks of tiles.

The row group's partial sums are accumulated in a loop-carried (1, 1) block, so any padded
width (multiple of 32) fits in L1. Chunk width is chosen at trace time to divide the width.
"""
import ttl
import ttnn


@ttl.operation(grid=(1, 1))
def ragged_sum(x, valid, out):
    assert x.shape[0] % 32 == 0 and x.shape[1] % 32 == 0
    assert valid.shape == x.shape and out.shape[0] == x.shape[0] and out.shape[1] == 32
    row_groups = x.shape[0] // 32
    width_tiles = x.shape[1] // 32
    chunk = next(c for c in (8, 4, 2, 1) if width_tiles % c == 0)
    chunks = width_tiles // chunk
    x_dfb = ttl.make_dataflow_buffer_like(x, shape=(1, chunk), block_count=2)
    valid_dfb = ttl.make_dataflow_buffer_like(valid, shape=(1, chunk), block_count=2)
    out_dfb = ttl.make_dataflow_buffer_like(out, shape=(1, 1), block_count=2)

    @ttl.compute()
    def compute():
        for _ in range(row_groups):
            with out_dfb.reserve() as out_blk:
                acc = ttl.block.fill(0.0, shape=(1, 1), dtype=ttnn.float32)
                for _c in range(chunks):
                    with x_dfb.wait() as x_blk, valid_dfb.wait() as valid_blk:
                        acc += ttl.math.reduce_sum(x_blk * valid_blk, dims=[1], shape=(1, 1))
                out_blk.store(acc)

    @ttl.datamovement()
    def reader():
        for row in range(row_groups):
            for c in range(chunks):
                with x_dfb.reserve() as x_blk, valid_dfb.reserve() as valid_blk:
                    tx_x = ttl.copy(x[row:row + 1, c * chunk:(c + 1) * chunk], x_blk)
                    tx_valid = ttl.copy(valid[row:row + 1, c * chunk:(c + 1) * chunk], valid_blk)
                    tx_x.wait()
                    tx_valid.wait()

    @ttl.datamovement()
    def writer():
        for row in range(row_groups):
            with out_dfb.wait() as out_blk:
                ttl.copy(out_blk, out[row:row + 1, 0:1]).wait()
