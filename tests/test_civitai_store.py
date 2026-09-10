"""Civitai 本地库单测:搜索页/详情的落库与命中,打桩 get_json 不触网。

依赖 tests 包内先行设置的 COMFYWEB_DATA_DIR 隔离环境(与 test_api_smoke 一致)。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("COMFYWEB_DATA_DIR", tempfile.mkdtemp(prefix="comfyweb-civtest-"))

import civitai  # noqa: E402
import db  # noqa: E402

FAKE_SEARCH = {"items": [
    {"id": 9001, "name": "T1", "type": "Checkpoint",
     "creator": {"username": "a"}, "nsfw": False,
     "base": "SD 1.5", "downloads": 5, "likes": 1, "cover": "", "modelVersions": []},
    {"id": 9002, "name": "T2", "type": "Checkpoint",
     "creator": {"username": "b"}, "nsfw": False,
     "base": "SDXL 1.0", "downloads": 3, "likes": 0, "cover": "", "modelVersions": []},
], "metadata": {"nextCursor": "CUR1"}}

FAKE_DETAIL = {"id": 9001, "name": "T1", "type": "Checkpoint",
               "creator": {"username": "a"},
               "tags": ["t"], "downloads": 5, "likes": 1, "description": "<p>用法</p>",
               "modelVersions": []}


class TestCivStore(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()  # 单独运行本模块时建表

    def setUp(self):
        db.execute("DELETE FROM civ_searches")
        db.execute("DELETE FROM civ_models WHERE civ_id IN (9001, 9002)")

    def tearDown(self):
        civitai.get_json = self._orig_get_json

    def setUp_patched(self, payload):
        self._orig_get_json = civitai.get_json
        civitai.get_json = lambda *a, **k: payload

    def test_search_miss_fetches_then_hit_serves_local(self):
        self.setUp_patched(FAKE_SEARCH)
        res = civitai.search_local(types=("Checkpoint",), sort="Most Downloaded")
        self.assertFalse(res["cached"])
        self.assertEqual([c["id"] for c in res["items"]], [9001, 9002])
        page = db.query_one("SELECT ids, next_cursor FROM civ_searches")
        self.assertIsNotNone(page)
        self.assertEqual(json.loads(page["ids"]), [9001, 9002])
        self.assertEqual(page["next_cursor"], "CUR1")

        calls = {"n": 0}
        def no_net(*a, **k):
            calls["n"] += 1
            raise AssertionError("命中本地库不应再触网")
        civitai.get_json = no_net
        res2 = civitai.search_local(types=("Checkpoint",), sort="Most Downloaded")
        self.assertTrue(res2["cached"])
        self.assertEqual([c["id"] for c in res2["items"]], [9001, 9002])
        self.assertEqual(calls["n"], 0)

    def test_search_refresh_bypasses_store(self):
        self.setUp_patched(FAKE_SEARCH)
        civitai.search_local(types=("Checkpoint",), sort="Most Downloaded")
        res = civitai.search_local(types=("Checkpoint",), sort="Most Downloaded",
                                   refresh=True)
        self.assertFalse(res["cached"])
        self.assertEqual([c["id"] for c in res["items"]], [9001, 9002])

    def test_detail_miss_fetches_stores_then_hit(self):
        self.setUp_patched(FAKE_DETAIL)
        d = civitai.get_model_local(9001)
        self.assertEqual(d["name"], "T1")
        row = db.query_one("SELECT detail_json FROM civ_models WHERE civ_id=9001")
        self.assertIsNotNone(row["detail_json"])  # 回归:曾因 SELECT 漏列存库前抛 IndexError

        def no_net(*a, **k):
            raise AssertionError("命中本地库不应再触网")
        civitai.get_json = no_net
        d2 = civitai.get_model_local(9001)
        self.assertEqual(d2["description"], "<p>用法</p>")

    def test_detail_refresh_refetches(self):
        self.setUp_patched(FAKE_DETAIL)
        civitai.get_model_local(9001)
        updated = dict(FAKE_DETAIL, description="<p>新版用法</p>")
        self.setUp_patched(updated)
        d = civitai.get_model_local(9001, refresh=True)
        self.assertIn("新版", d["description"])


if __name__ == "__main__":
    unittest.main()
