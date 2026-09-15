"""hoststats 解析单测:nvidia-smi 单行输出的字段拆分。"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tempfile
os.environ.setdefault("COMFYWEB_DATA_DIR", tempfile.mkdtemp(prefix="comfyweb-hs-"))

from hoststats import _map_hwapi, _parse_gpu_line  # noqa: E402


class TestParseGpuLine(unittest.TestCase):
    def test_parse_basic(self):
        self.assertEqual(_parse_gpu_line("65, 43, 3456, 8192"),
                         {"temp": 65, "util": 43, "vram_used": 3456, "vram_total": 8192})

    def test_parse_no_space(self):
        self.assertEqual(_parse_gpu_line("72,100,8192,8192"),
                         {"temp": 72, "util": 100, "vram_used": 8192, "vram_total": 8192})


if __name__ == "__main__":
    unittest.main()


class TestMapHwapi(unittest.TestCase):
    def test_map_full(self):
        st = {"gpus": [{"name": "RTX 3060 Ti", "utilization": 1, "temperature": 46,
                        "vram_total": 8589934592, "vram_used": 5018284032,
                        "vram_free": 3571650560}],
              "gpu_temperature": 46, "gpu_utilization": 1,
              "vram_used": 5018284032, "vram_total": 8589934592,
              "cpu_temperature": None, "cpu_source": "unavailable"}
        d = _map_hwapi(st)
        self.assertEqual(d["temp"], 46)
        self.assertEqual(d["util"], 1)
        self.assertEqual(d["vram_used"], 4785)   # 字节 → MB
        self.assertEqual(d["vram_total"], 8192)
        self.assertNotIn("cpu_temp", d)          # 无数据源不造字段

    def test_map_with_cpu_temp(self):
        st = {"gpu_temperature": 55, "gpu_utilization": 30,
              "vram_used": 1, "vram_total": 2, "cpu_temperature": 61}
        self.assertEqual(_map_hwapi(st)["cpu_temp"], 61)


if __name__ == "__main__":
    unittest.main()
