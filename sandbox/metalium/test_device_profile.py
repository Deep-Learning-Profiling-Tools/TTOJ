"""Meaningful parser fixtures: overlap, stream damage and unknown frequency."""
import importlib.util
from pathlib import Path
import unittest

module_path = Path(__file__).resolve().parents[2] / "worker/device_profile.py"
spec = importlib.util.spec_from_file_location("device_profile", module_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
parse = module.parse_device_profile

HEADER = "PCIe slot, core_x, core_y, RISC processor type, timer_id, time[cycles since reset], data, run host ID, trace id, trace id counter, zone name, type, source line, source file, meta data\n"


def marker(risc, timestamp, phase, run=32, device=0):
    name = "TRISC-KERNEL" if risc.startswith("TRISC") else f"{risc}-KERNEL"
    return f"{device},1,1,{risc},123,{timestamp},0,{run},,,{name},{phase},77,firmware.cc,\n"


class DeviceProfileTests(unittest.TestCase):
    def test_parallel_riscs_are_not_summed_and_absolute_cycles_remain(self):
        raw = "ARCH: blackhole, CHIP_FREQ[MHz]: 1000\n" + HEADER
        raw += marker("BRISC", 1000, "ZONE_START") + marker("BRISC", 1100, "ZONE_END")
        raw += marker("NCRISC", 1020, "ZONE_START") + marker("NCRISC", 1080, "ZONE_END")
        raw += marker("TRISC_1", 1030, "ZONE_START") + marker("TRISC_1", 1070, "ZONE_END")
        result = parse(raw.encode())
        self.assertTrue(result["valid"])
        self.assertEqual([x["duration_cycles"] for x in result["kernels"]], [100, 60, 40])
        self.assertEqual(result["core_spans"][0]["duration_cycles"], 100)
        self.assertEqual(result["core_spans"][0]["duration_ns"], 100)
        self.assertEqual(result["kernels"][0]["start_cycle"], 1000)

    def test_truncated_and_misordered_markers_are_explicit(self):
        raw = "ARCH: blackhole, CHIP_FREQ[MHz]: 1350\n" + HEADER
        raw += marker("BRISC", 100, "ZONE_START") + marker("BRISC", 90, "ZONE_END")
        raw += marker("NCRISC", 200, "ZONE_START")
        result = parse(raw)
        self.assertFalse(result["valid"])
        reasons = {x["reason"] for x in result["errors"]}
        self.assertTrue({"misordered_timestamp", "end_precedes_begin", "unpaired_begin_marker"} <= reasons)

    def test_unknown_clock_preserves_cycles_and_multiple_devices_remain_separate(self):
        raw = "ARCH: blackhole\n" + HEADER
        for device, start in ((0, 100), (1, 10000)):
            raw += marker("BRISC", start, "ZONE_START", device=device)
            raw += marker("BRISC", start + 42, "ZONE_END", device=device)
        result = parse(raw)
        self.assertTrue(result["valid"])
        self.assertIsNone(result["clock_mhz"])
        self.assertEqual(len(result["core_spans"]), 2)
        self.assertEqual([x["duration_cycles"] for x in result["kernels"]], [42, 42])
        self.assertTrue(all(x["duration_ns"] is None for x in result["kernels"]))

    def test_byte_ranges_select_repeats_even_when_run_id_is_reused(self):
        raw = "ARCH: blackhole, CHIP_FREQ[MHz]: 1000\n" + HEADER
        raw += marker("BRISC", 1000, "ZONE_START") + marker("BRISC", 1999, "ZONE_END")
        ranges = []
        for sample, start, duration in ((1, 2000, 100), (2, 3000, 120)):
            begin = len(raw.encode())
            raw += marker("BRISC", start, "ZONE_START") + marker("BRISC", start + duration, "ZONE_END")
            raw += marker("TRISC_1", start + 20, "ZONE_START") + marker("TRISC_1", start + 80, "ZONE_END")
            ranges.append({"sample": sample, "start_byte": begin, "end_byte": len(raw.encode())})
        capture = {"selection": "csv_append_byte_ranges", "warmup": 5, "sample_count": 2, "sample_byte_ranges": ranges}
        result = parse(raw, capture=capture)
        self.assertTrue(result["valid"])
        self.assertEqual(result["raw_kernel_count"], 5)
        self.assertEqual(len(result["kernels"]), 4)
        self.assertEqual([span["duration_cycles"] for span in result["core_spans"]], [100, 120])
        self.assertEqual(result["statistics"][0]["count"], 2)
        self.assertEqual(result["statistics"][0]["median_cycles"], 110)
        self.assertEqual(result["statistics"][0]["p95_cycles"], 119)

    def test_malformed_capture_never_falls_back_to_all_kernels(self):
        raw = "ARCH: blackhole, CHIP_FREQ[MHz]: 1000\n" + HEADER
        raw += marker("BRISC", 1, "ZONE_START") + marker("BRISC", 10, "ZONE_END")
        for capture in ({"selection": "csv_append_byte_ranges", "sample_count": 1, "sample_byte_ranges": []},
                        {"selection": "none"}, {}):
            with self.subTest(capture=capture):
                result = parse(raw, capture=capture)
                self.assertFalse(result["valid"])
                self.assertEqual(result["kernels"], [])
                self.assertEqual(result["errors"][0]["reason"], "invalid_capture_metadata")

    def test_multiple_programs_are_summarized_as_separate_ordered_stages(self):
        raw = "ARCH: blackhole, CHIP_FREQ[MHz]: 1000\n" + HEADER
        ranges = []
        for sample, base in ((1, 1000), (2, 2000)):
            begin = len(raw.encode())
            for start, duration in ((base, 100 + sample), (base + 500, 400 + sample)):
                raw += marker("BRISC", start, "ZONE_START") + marker("BRISC", start + duration, "ZONE_END")
            ranges.append({"sample": sample, "start_byte": begin, "end_byte": len(raw.encode())})
        capture = {"selection": "csv_append_byte_ranges", "warmup": 5, "sample_count": 2, "sample_byte_ranges": ranges}
        result = parse(raw, capture)
        self.assertTrue(result["valid"])
        self.assertEqual([x["program_index"] for x in result["statistics"]], [1, 2])
        self.assertEqual([x["count"] for x in result["statistics"]], [2, 2])
        self.assertEqual([x["median_cycles"] for x in result["statistics"]], [101.5, 401.5])
        self.assertTrue(all(x["complete"] for x in result["statistics"]))
        self.assertEqual(len(result["core_spans"]), 4)


if __name__ == "__main__":
    unittest.main()
