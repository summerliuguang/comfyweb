"""library.py 单元测试:分类路径/分片只增不改名/索引/收编/快照/还原。

使用独立临时数据目录(COMFYWEB_DATA_DIR)与临时 library 目录,绝不触碰生产与 NAS。
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["COMFYWEB_DATA_DIR"] = tempfile.mkdtemp(prefix="comfyweb-lib-")

import db  # noqa: E402
import library  # noqa: E402

db.init_db()


class TestNamesAndClassify(unittest.TestCase):
    def test_safe_name(self):
        # 尾部的下划线/点会被 strip(目录名整洁),中间的替换为 _
        self.assertEqual(library._safe_name('a/b\\c:d*?"<>|x'), "a_b_c_d______x")
        self.assertEqual(library._safe_name("修仙 角色套图 "), "修仙 角色套图")
        self.assertEqual(library._safe_name(""), "未命名")

    def test_model_stem_and_prefix(self):
        self.assertEqual(library._model_stem("anima_turboV11.safetensors"), "anima_turboV11")
        self.assertEqual(library._prefix_of("batchgen_00123_.png"), "batchgen")
        self.assertEqual(library._prefix_of("Anima_00007_.png"), "Anima")

    def test_classify_known_and_unknown(self):
        meta = {"model": "anima_turboV11.safetensors", "category": "立绘",
                "batch": "修仙套图", "workflow": "", "created_at": "2026-09-19 10:00:00"}
        parent, tags = library.classify("batchgen_001_.png", meta)
        self.assertEqual(str(parent), "anima_turboV11/2026-09-19_修仙套图")
        self.assertIn("立绘", tags)
        parent, tags = library.classify("cos_tmp_001_.png", None)
        self.assertEqual(str(parent), "未分类/cos_tmp")
        self.assertEqual(tags, ["未分类", "cos_tmp"])

    def test_classify_rejects_unsafe_theme(self):
        meta = {"model": "m.safetensors", "batch": "../../etc", "created_at": "2026-09-19"}
        parent, _ = library.classify("x.png", meta)
        self.assertNotIn("..", str(parent))


class TestShard(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="lib-shard-"))
        self.old_limit = library.SHARD_LIMIT
        library.SHARD_LIMIT = 3  # 测试用小阈值

    def tearDown(self):
        library.SHARD_LIMIT = self.old_limit

    def test_shard_grows_but_never_renames(self):
        base = self.root / "model" / "2026-09-19_任务"
        files = []
        for i in range(6):
            d = library._shard_dir(base)
            f = d / f"img_{i}.png"
            d.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"x")
            files.append(f)
        # 前 3 张进 base,后续进 _002;base 里最初的文件名与路径永不变化
        self.assertEqual(len(list(base.iterdir())), 3)
        self.assertTrue(files[0].exists() and files[0] == base / "img_0.png")
        self.assertTrue(files[3].parent.name.endswith("_002"))
        self.assertTrue(files[5].parent.name.endswith("_002"))


class TestPlaceAndCollect(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="lib-"))
        self.out = self.root / "output"
        self.out.mkdir()
        db.set_setting("library_dir", str(self.root / "library"))
        # 一条可归属的任务记录(文件名 → 任务)
        cur = db.execute(
            "INSERT INTO tasks(model, batch, category, prompt_text, seed) "
            "VALUES('anima_turboV11.safetensors', '测试批次', '立绘', 'p', 1)")
        tid = cur.lastrowid
        db.execute("INSERT INTO images(task_id, filename, type) VALUES(?, 'known_001_.png', 'output')",
                   (tid,))

    def tearDown(self):
        db.set_setting("library_dir", "")

    def test_place_indexes_and_dedups(self):
        src = Path(tempfile.mkdtemp()) / "known_001_.png"
        src.write_bytes(b"PNGDATA")
        rel = library.place(src, "known_001_.png")
        p = library.library_dir() / rel
        self.assertTrue(p.exists())
        self.assertEqual(p.read_bytes(), b"PNGDATA")
        row = library.indexed("known_001_.png")
        self.assertIn("anima_turboV11", row["path"])
        self.assertEqual(row["source"], "archive")
        # 再次放置:同尺寸不重复复制,同名索引仍一行(库可能被同进程其他用例共享)
        rel2 = library.place(src, "known_001_.png")
        self.assertEqual(rel, rel2)
        conn = library._lib_connect()
        n = conn.execute(
            "SELECT COUNT(*) FROM files WHERE filename='known_001_.png'").fetchone()[0]
        conn.close()
        self.assertEqual(n, 1)

    def test_collect_moves_and_dedups(self):
        (self.out / "known_001_.png").write_bytes(b"PNGDATA2")
        (self.out / "unknown_001_.png").write_bytes(b"UNK")
        sub = self.out / "fanren"
        sub.mkdir()
        (sub / "manual.png").write_bytes(b"MANUAL")  # 手工子目录,绝不收编
        moved, skipped = library.collect_once(limit=10)
        self.assertEqual(moved, 2)
        self.assertFalse((self.out / "known_001_.png").exists())
        self.assertTrue((sub / "manual.png").exists())  # 子目录未动
        row = library.indexed("unknown_001_.png")
        self.assertTrue(row["path"].startswith("未分类/unknown"))
        # GPU 同步重复推送同名文件:已入索引,跳过不重复收编
        (self.out / "known_001_.png").write_bytes(b"PNGDATA-dup")
        (self.out / "unknown_001_.png").write_bytes(b"UNK-dup")
        moved2, skipped2 = library.collect_once(limit=10)
        self.assertEqual(moved2, 0)
        self.assertEqual(skipped2, 2)

    def test_reverse_restores(self):
        src = Path(tempfile.mkdtemp()) / "known_001_.png"
        src.write_bytes(b"PNGDATA")
        library.place(src, "known_001_.png")
        self.assertTrue(library.reverse("known_001_.png"))
        self.assertTrue((self.out / "known_001_.png").exists())
        self.assertIsNone(library.indexed("known_001_.png"))

    def test_snapshot_md5(self):
        src = Path(tempfile.mkdtemp()) / "known_001_.png"
        src.write_bytes(b"PNGDATA")
        library.place(src, "known_001_.png")
        self.assertTrue(library.snapshot())
        # 快照与正本内容一致(WAL 库裸字节不稳定,按行数比对)
        def count(p):
            c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            n = c.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            c.close()
            return n
        self.assertEqual(count(library.library_dir() / "index.db"),
                         count(db.DATA_DIR / "library.db"))

    def test_find_on_nas(self):
        src = Path(tempfile.mkdtemp()) / "known_001_.png"
        src.write_bytes(b"PNGDATA")
        library.place(src, "known_001_.png")
        self.assertEqual(library.find_on_nas("known_001_.png"),
                         library.library_dir() / library.indexed("known_001_.png")["path"])
        # 未收编但还在 output 根:原位命中
        (self.out / "loose_001_.png").write_bytes(b"L")
        self.assertEqual(library.find_on_nas("loose_001_.png"),
                         self.out / "loose_001_.png")
        self.assertIsNone(library.find_on_nas("nope_999_.png"))


if __name__ == "__main__":
    unittest.main()
