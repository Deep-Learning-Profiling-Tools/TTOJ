"""Wrong on purpose: ignores the valid mask in the denominator.

"""Reference solution: single core, three passes per group of 32 rows, one tile at a time.

Pass 1 finds the masked row maximum, pass 2 accumulates the denominator, pass 3 writes the
normalized weights. Row statistics are kept in loop-carried (1, 1) blocks and handed to the
broadcasts through scratch dataflow buffers. Masked entries are replaced by -300 so that
exp(masked - max) underflows to zero; empty rows get a denominator of 1 and therefore zeros.
"""
import ttl
import ttnn


@ttl.operation(grid=(1, 1))
def ragged_softmax(x, valid, out):
    assert x.shape[0] % 32 == 0 and x.shape[1] % 32 == 0
    assert valid.shape == x.shape and out.shape == x.shape
    row_groups = x.shape[0] // 32
    width_tiles = x.shape[1] // 32
    x_dfb = ttl.make_dataflow_buffer_like(x, shape=(1, 1), block_count=2)
    valid_dfb = ttl.make_dataflow_buffer_like(valid, shape=(1, 1), block_count=2)
    out_dfb = ttl.make_dataflow_buffer_like(out, shape=(1, 1), block_count=2)
    z_dfb = ttl.make_dataflow_buffer_like(out, shape=(1, 1), block_count=2)      # masked logits for reduce_max
    w_dfb = ttl.make_dataflow_buffer_like(out, shape=(1, 1), block_count=2)      # weights for reduce_sum
    max_dfb = ttl.make_dataflow_buffer_like(out, shape=(1, 1), block_count=2)    # row maxima for broadcast
    inv_dfb = ttl.make_dataflow_buffer_like(out, shape=(1, 1), block_count=2)    # 1 / denominator for broadcast

    @ttl.compute()
    def compute():
        for _ in range(row_groups):
            ones = ttl.block.fill(1.0, shape=(1, 1), dtype=ttnn.float32)
            sentinel = ttl.block.fill(-300.0, shape=(1, 1), dtype=ttnn.float32)
            # Pass 1: running maximum of the masked logits, kept as max(m, r) = m + max(r - m, 0).
            row_max = ttl.block.fill(-300.0, shape=(1, 1), dtype=ttnn.float32)
            zero = ttl.block.fill(0.0, shape=(1, 1), dtype=ttnn.float32)
            for _t in range(width_tiles):
                with x_dfb.wait() as x_blk, valid_dfb.wait() as valid_blk, z_dfb.reserve() as z_blk:
                    z_blk.store(x_blk * valid_blk + sentinel * (ones - valid_blk))
                with z_dfb.wait() as z_blk:
                    row_max += ttl.max(ttl.math.reduce_max(z_blk, dims=[1], shape=(1, 1)) - row_max, zero)
            with max_dfb.reserve() as max_blk:
                max_blk.store(row_max)
            with max_dfb.wait() as max_blk:
                shift = ttl.block.broadcast(max_blk, dims=[1], shape=(1, 1))
                # Pass 2: denominator; an empty row sums to 0 and is normalized by 1 instead.
                denominator = ttl.block.fill(0.0, shape=(1, 1), dtype=ttnn.float32)
                for _t in range(width_tiles):
                    with x_dfb.wait() as x_blk, valid_dfb.wait() as valid_blk, w_dfb.reserve() as w_blk:
                        w_blk.store(ttl.exp(x_blk * valid_blk + sentinel * (ones - valid_blk) - shift) * valid_blk)
                    with w_dfb.wait() as w_blk:
                        denominator += ttl.math.reduce_sum(w_blk, dims=[1], shape=(1, 1)) + ones
                safe = denominator + (ones - ttl.min(denominator, ones))
                # ttl.recip is a fast approximation (about 5% here); two Newton steps bring it to FPU precision.
                inverse = ttl.recip(safe)
                inverse = inverse * (ones + ones - safe * inverse)
                inverse = inverse * (ones + ones - safe * inverse)
                with inv_dfb.reserve() as inv_blk:
                    inv_blk.store(inverse)
                # Pass 3: normalized weights.
                with inv_dfb.wait() as inv_blk:
                    scale = ttl.block.broadcast(inv_blk, dims=[1], shape=(1, 1))
                    for _t in range(width_tiles):
                        with x_dfb.wait() as x_blk, valid_dfb.wait() as valid_blk, out_dfb.reserve() as out_blk:
                            out_blk.store(ttl.exp(x_blk * valid_blk + sentinel * (ones - valid_blk) - shift) * valid_blk * scale)

    @ttl.datamovement()
    def reader():
        for row in range(row_groups):
            for _pass in range(3):
                for t in range(width_tiles):
                    with x_dfb.reserve() as x_blk, valid_dfb.reserve() as valid_blk:
                        tx_x = ttl.copy(x[row:row + 1, t:t + 1], x_blk)
                        tx_valid = ttl.copy(valid[row:row + 1, t:t + 1], valid_blk)
                        tx_x.wait()
                        tx_valid.wait()

    @ttl.datamovement()
    def writer():
        for row in range(row_groups):
            for t in range(width_tiles):
                with out_dfb.wait() as out_blk:
                    ttl.copy(out_blk, out[row:row + 1, t:t + 1]).wait()
