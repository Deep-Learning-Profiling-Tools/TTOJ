"""One device-profiler capture; run under the OJ device lock."""
import json
import os
import pathlib
import sys
import numpy as np
import ttnn

operation = sys.argv[1]
device = ttnn.open_device(device_id=0)
try:
    device.enable_program_cache()
    values = np.full((32, 32), 2, dtype=np.float32)
    a = ttnn.Tensor(values, ttnn.bfloat16, device, ttnn.TILE_LAYOUT)
    b = ttnn.Tensor(values, ttnn.bfloat16, device, ttnn.TILE_LAYOUT)
    result = ttnn.add(a, b) if operation == "add" else ttnn.matmul(a, b)
    ttnn.synchronize_device(device)
    host = ttnn.to_dtype(ttnn.to_layout(ttnn.from_device(result), ttnn.ROW_MAJOR_LAYOUT), ttnn.float32)
    output = np.asarray(host.to_numpy(), dtype=np.float32)
    expected = 4 if operation == "add" else 128
    if not np.all(output == expected):
        raise RuntimeError("Incorrect hardware result")
    ttnn._ttnn.device.ReadDeviceProfiler(device)
finally:
    ttnn.close_device(device)
csv_path = pathlib.Path(os.environ["TT_METAL_PROFILER_DIR"]) / ".logs/profile_log_device.csv"
print(json.dumps({"operation": operation, "correct": True, "csv_path": str(csv_path),
                  "csv_bytes": csv_path.stat().st_size, "csv_header": csv_path.open().readline().strip()}))
