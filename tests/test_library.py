"""library.py 单元测试:分类路径/分片只增不改名/索引/收编/快照/还原/摄取/缩略图。

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
# 本进程所有 library 测试共用一个临时整理库(名字必须是 library,nas_root 相对路径才成立);
# 绝不还原为空(空 = 默认真实 NAS)
_LIB_TMP = Path(tempfile.mkdtemp(prefix="comfyweb-libroot-"))
db.set_setting("library_dir", str(_LIB_TMP / "library"))


def _make_png(path, color=(200, 30, 40), size=(64, 48)):
    from PIL import Image
    img = Image.new("RGB", size, color)
    img.save(path)


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
        self.old_limit = library.SHARD_LIMIT
        library.SHARD_LIMIT = 3  # 测试用小阈值

    def tearDown(self):
        library.SHARD_LIMIT = self.old_limit

    def test_shard_grows_but_never_renames(self):
        base = library.library_dir() / "shardmodel" / "2026-09-19_任务"
        files = []
        for i in range(6):
            d = library._shard_dir(base)
            d.mkdir(parents=True, exist_ok=True)
            f = d / f"img_{i}.png"
            f.write_bytes(b"x")
            files.append(f)
        # 前 3 张进 base,后续进 _002;base 里最初的文件名与路径永不变化
        self.assertEqual(len(list(base.iterdir())), 3)
        self.assertTrue(files[0].exists() and files[0] == base / "img_0.png")
        self.assertTrue(files[3].parent.name.endswith("_002"))
        self.assertTrue(files[5].parent.name.endswith("_002"))


class TestPlaceAndCollect(unittest.TestCase):
    def setUp(self):
        self.out = library.output_dir()
        self.out.mkdir(parents=True, exist_ok=True)
        # 两条可归属的任务记录(collect 用 known,place 用 placed)
        self.tid = db.execute(
            "INSERT INTO tasks(model, batch, category, prompt_text, seed) "
            "VALUES('anima_turboV11.safetensors', '测试批次', '立绘', 'p', 1)").lastrowid
        db.execute("INSERT INTO images(task_id, filename, type) VALUES(?, 'known_001_.png', 'output')",
                   (self.tid,))
        self.tid2 = db.execute(
            "INSERT INTO tasks(model, batch, category, prompt_text, seed) "
            "VALUES('anima_turboV11.safetensors', '测试批次', '立绘', 'p2', 2)").lastrowid
        db.execute("INSERT INTO images(task_id, filename, type) VALUES(?, 'placed_001_.png', 'output')",
                   (self.tid2,))

    def tearDown(self):
        db.execute("DELETE FROM images WHERE task_id IN (?,?)", (self.tid, self.tid2))
        db.execute("DELETE FROM tasks WHERE id IN (?,?)", (self.tid, self.tid2))
        for fn in ("known_001_.png", "placed_001_.png"):
            library._index_remove(fn)

    def test_place_indexes_and_dedups(self):
        src = Path(tempfile.mkdtemp()) / "placed_001_.png"
        src.write_bytes(b"PNGDATA")
        rel = library.place(src, "placed_001_.png")
        p = library.nas_root() / rel
        self.assertTrue(p.exists())
        self.assertEqual(p.read_bytes(), b"PNGDATA")
        row = library.indexed("placed_001_.png")
        self.assertTrue(row["path"].startswith("library/anima_turboV11"))
        self.assertEqual(row["source"], "archive")
        # 再次放置:同尺寸不重复复制,同名索引仍一行(库可能被同进程其他用例共享)
        rel2 = library.place(src, "placed_001_.png")
        self.assertEqual(rel, rel2)
        conn = library._lib_connect()
        n = conn.execute(
            "SELECT COUNT(*) FROM files WHERE filename='placed_001_.png'").fetchone()[0]
        conn.close()
        self.assertEqual(n, 1)

    def test_collect_moves_and_dedups(self):
        (self.out / "known_001_.png").write_bytes(b"PNGDATA2")
        (self.out / "unknown_001_.png").write_bytes(b"UNK")
        sub = self.out / "fanren"
        sub.mkdir(exist_ok=True)
        (sub / "manual.png").write_bytes(b"MANUAL")  # 手工子目录,绝不收编
        moved, skipped = library.collect_once(limit=10)
        self.assertEqual(moved, 2)
        self.assertFalse((self.out / "known_001_.png").exists())
        self.assertTrue((sub / "manual.png").exists())  # 子目录未动
        row = library.indexed("unknown_001_.png")
        self.assertTrue(row["path"].startswith("library/未分类/unknown"))
        # GPU 同步重复推送同名文件:已入索引,跳过不重复收编
        (self.out / "known_001_.png").write_bytes(b"PNGDATA-dup")
        (self.out / "unknown_001_.png").write_bytes(b"UNK-dup")
        moved2, skipped2 = library.collect_once(limit=10)
        self.assertEqual(moved2, 0)
        self.assertEqual(skipped2, 2)

    def test_reverse_restores(self):
        src = Path(tempfile.mkdtemp()) / "placed_001_.png"
        src.write_bytes(b"PNGDATA")
        library.place(src, "placed_001_.png")
        self.assertTrue(library.reverse("placed_001_.png"))
        self.assertTrue((self.out / "placed_001_.png").exists())
        self.assertIsNone(library.indexed("placed_001_.png"))

    def test_snapshot_md5(self):
        src = Path(tempfile.mkdtemp()) / "placed_001_.png"
        src.write_bytes(b"PNGDATA")
        library.place(src, "placed_001_.png")
        self.assertTrue(library.snapshot())

        def count(p):
            c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            n = c.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            c.close()
            return n
        self.assertEqual(count(library.library_dir() / "index.db"),
                         count(db.DATA_DIR / "library.db"))

    def test_find_on_nas(self):
        src = Path(tempfile.mkdtemp()) / "placed_001_.png"
        src.write_bytes(b"PNGDATA")
        library.place(src, "placed_001_.png")
        self.assertEqual(library.find_on_nas("placed_001_.png"),
                         library.nas_root() / library.indexed("placed_001_.png")["path"])
        # 未收编但还在 output 根:原位命中
        (self.out / "loose_001_.png").write_bytes(b"L")
        self.assertEqual(library.find_on_nas("loose_001_.png"),
                         self.out / "loose_001_.png")
        self.assertIsNone(library.find_on_nas("nope_999_.png"))


class TestLibV2(unittest.TestCase):
    """v2 索引:迁移、全库摄取、Pillow 缩略图、分页查询。"""

    def test_v1_to_v2_migration(self):
        """v1(文件名主键、library 根相对路径)→ v2(path 主键、加 library 段前缀)。"""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE files(
            filename TEXT PRIMARY KEY, path TEXT NOT NULL, thumb TEXT,
            model TEXT DEFAULT '', category TEXT DEFAULT '', batch TEXT DEFAULT '',
            workflow TEXT DEFAULT '', prompt TEXT DEFAULT '', seed INTEGER,
            tags TEXT DEFAULT '[]', size INTEGER DEFAULT 0, source TEXT DEFAULT '',
            created_at TEXT DEFAULT '2026-09-19')""")
        conn.execute("INSERT INTO files(filename, path, size) "
                     "VALUES('old.png', 'm/2026_任务/old.png', 5)")
        library._migrate_v1(conn, "library")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(files)")}
        self.assertIn("path", cols)
        self.assertIn("lora", cols)
        row = conn.execute("SELECT * FROM files WHERE filename='old.png'").fetchone()
        self.assertEqual(row["path"], "library/m/2026_任务/old.png")
        self.assertEqual(row["size"], 5)
        conn.close()

    def test_ingest_refresh(self):
        """output/ 手工子目录原地索引:不移动文件,有任务记录的补元数据,增量刷新。"""
        out = library.output_dir()
        proj = out / "myproj"
        proj.mkdir(parents=True, exist_ok=True)
        (proj / "art_001_.png").write_bytes(b"IMG1")
        (proj / "art_002_.png").write_bytes(b"IMG2")
        # art_001 挂一条任务记录(元数据增强)
        cur = db.execute(
            "INSERT INTO tasks(model, batch, prompt_text) VALUES('m1.safetensors', '批次X', 'p')")
        db.execute("INSERT INTO images(task_id, filename, type) VALUES(?, 'art_001_.png', 'output')",
                   (cur.lastrowid,))
        changed = library.ingest_refresh()
        self.assertGreaterEqual(changed, 2)
        row = library.indexed("art_001_.png")
        self.assertTrue(row["path"].startswith("output/myproj/"))
        self.assertEqual(row["model"], "m1.safetensors")   # 任务元数据增强
        row2 = library.indexed("art_002_.png")
        self.assertEqual(row2["model"], "myproj")          # 无记录 → 目录名
        self.assertTrue((proj / "art_001_.png").exists())  # 原地不动
        # 无变更再扫:零写入
        self.assertEqual(library.ingest_refresh(), 0)
        library._index_remove("art_001_.png")
        library._index_remove("art_002_.png")

    def test_thumb_generate(self):
        src = Path(tempfile.mkdtemp()) / "thumbtest_001_.png"
        _make_png(src, size=(1024, 768))
        library.place(src, "thumbtest_001_.png")
        row = library.indexed("thumbtest_001_.png")
        self.assertIsNone(row["thumb"])
        body = library.thumb_generate(row)
        self.assertIsNotNone(body)
        row = library.indexed("thumbtest_001_.png")  # 重新取:生成后索引已更新
        self.assertIsNotNone(row["thumb"])
        t = library.nas_root() / row["thumb"]
        self.assertTrue(t.exists() and t.stat().st_size > 0)
        from PIL import Image
        with Image.open(t) as im:
            self.assertLessEqual(max(im.size), 512)
        library._index_remove("thumbtest_001_.png")

    def test_page_query_filters(self):
        # 自种两行(不同模型/批次),避免依赖其他用例的写入顺序
        for fn, model, batch in (("pq_a_001_.png", "anima_turboV11.safetensors", "筛选批次A"),
                                 ("pq_b_001_.png", "z-image-turbo.safetensors", "筛选批次B")):
            cur = db.execute(
                "INSERT INTO tasks(model, batch) VALUES(?, ?)", (model, batch))
            db.execute("INSERT INTO images(task_id, filename, type) VALUES(?, ?, 'output')",
                       (cur.lastrowid, fn))
            f = Path(tempfile.mkdtemp()) / fn
            f.write_bytes(b"D")
            library.place(f, fn)
        rows, total = library.page_query("model LIKE ?", ("%anima_turboV11%",), 1, 5)
        self.assertGreaterEqual(total, 1)
        self.assertTrue(all("anima" in r["model"].lower() for r in rows))
        rows2, total2 = library.page_query("batch LIKE ?", ("%筛选批次B%",), 1, 10)
        self.assertGreaterEqual(total2, 1)
        _, total_all = library.page_query("", (), 1, 5)
        self.assertGreaterEqual(total_all, total2)


if __name__ == "__main__":
    unittest.main()
