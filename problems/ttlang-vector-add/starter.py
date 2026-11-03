"""CS540 · TT-Lang vector addition starter.

The judge calls vector_add(a, b, out) with FP32 tile-layout DRAM tensors of shape
(P / 32, 32), where P = 1024 * ceil(N / 1024); only the first N values of out are checked.
"""
import ttl


@ttl.operation(grid=(1, 1))
def vector_add(a, b, out):
    tiles = a.shape[0] // 32
    a_dfb = ttl.make_dataflow_buffer_like(a, shape=(1, 1), block_count=2)
    b_dfb = ttl.make_dataflow_buffer_like(b, shape=(1, 1), block_count=2)
    out_dfb = ttl.make_dataflow_buffer_like(out, shape=(1, 1), block_count=2)

    @ttl.compute()
    def compute():
        for _ in range(tiles):
            with a_dfb.wait() as a_blk, b_dfb.wait() as b_blk, out_dfb.reserve() as out_blk:
                out_blk.store(a_blk + b_blk)  # TODO: this is the one-tile baseline; make it fast

    @ttl.datamovement()
    def reader():
        for tile in range(tiles):
            with a_dfb.reserve() as a_blk, b_dfb.reserve() as b_blk:
                tx_a = ttl.copy(a[tile:tile + 1, 0:1], a_blk)
                tx_b = ttl.copy(b[tile:tile + 1, 0:1], b_blk)
                tx_a.wait()
                tx_b.wait()

    @ttl.datamovement()
    def writer():
        for tile in range(tiles):
            with out_dfb.wait() as out_blk:
                ttl.copy(out_blk, out[tile:tile + 1, 0:1]).wait()
