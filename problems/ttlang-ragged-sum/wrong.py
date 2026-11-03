import ttl
import ttnn


@ttl.operation(grid=(1, 1))
def ragged_sum(x, valid, out):
    """Wrong on purpose: ignores the valid mask."""
    row_groups = x.shape[0] // 32
    width_tiles = x.shape[1] // 32
    chunk = next(c for c in (8, 4, 2, 1) if width_tiles % c == 0)
    chunks = width_tiles // chunk
    x_dfb = ttl.make_dataflow_buffer_like(x, shape=(1, chunk), block_count=2)
    out_dfb = ttl.make_dataflow_buffer_like(out, shape=(1, 1), block_count=2)

    @ttl.compute()
    def compute():
        for _ in range(row_groups):
            with out_dfb.reserve() as out_blk:
                acc = ttl.block.fill(0.0, shape=(1, 1), dtype=ttnn.float32)
                for _c in range(chunks):
                    with x_dfb.wait() as x_blk:
                        acc += ttl.math.reduce_sum(x_blk, dims=[1], shape=(1, 1))
                out_blk.store(acc)

    @ttl.datamovement()
    def reader():
        for row in range(row_groups):
            for c in range(chunks):
                with x_dfb.reserve() as x_blk:
                    ttl.copy(x[row:row + 1, c * chunk:(c + 1) * chunk], x_blk).wait()

    @ttl.datamovement()
    def writer():
        for row in range(row_groups):
            with out_dfb.wait() as out_blk:
                ttl.copy(out_blk, out[row:row + 1, 0:1]).wait()
