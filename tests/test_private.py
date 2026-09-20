"""私密内容(NSFW)功能测试:密码流转、内容过滤、图片私密化移动。

使用独立临时数据目录(COMFYWEB_DATA_DIR)与临时 library 目录,绝不触碰生产与 NAS。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["COMFYWEB_DATA_DIR"] = tempfile.mkdtemp(prefix="comfyweb-nsfw-")

import db  # noqa: E402
import library  # noqa: E402

db.init_db()
_LIB_TMP = Path(tempfile.mkdtemp(prefix="comfyweb-nsfw-libroot-"))
db.set_setting("library_dir", str(_LIB_TMP / "library"))

import app as app_mod  # noqa: E402

app_mod.app.config["TESTING"] = True
client_app = app_mod.app.test_client()


def _png(path):
    from PIL import Image
    Image.new("RGB", (32, 24), (120, 30, 60)).save(path)


def _place(fn):
    f = Path(tempfile.mkdtemp()) / fn
    _png(f)
    library.place(f, fn)
    return library.indexed(fn)["rowid"]


class TestPrivatePassword(unittest.TestCase):
    def test_setup_unlock_lock_flow(self):
        db.set_setting("private_pw", "")   # 复位(N 类可能设过)
        db.set_setting("private_enabled", "0")
        s = client_app.get("/api/private/status").get_json()
        self.assertFalse(s["configured"])
        # 未设置密码时解锁被拒
        r = client_app.post("/api/private/unlock", json={"password": "x"})
        self.assertEqual(r.status_code, 400)
        # 设置 + 开启
        r = client_app.post("/api/private/setup", json={"password": "abcd"})
        self.assertEqual(r.status_code, 200)
        r = client_app.post("/api/private/unlock", json={"password": "abcd"})
        self.assertEqual(r.get_json()["enabled"], True)
        self.assertTrue(db.get_setting("private_enabled") == "1")
        # 错密码无法再次开启(先锁再开)
        client_app.post("/api/private/lock")
        r = client_app.post("/api/private/unlock", json={"password": "wrong"})
        self.assertEqual(r.status_code, 400)
        self.assertTrue(db.get_setting("private_enabled") != "1")
        r = client_app.post("/api/private/unlock", json={"password": "abcd"})
        self.assertEqual(r.get_json()["enabled"], True)
        # 改密码需旧密码
        r = client_app.post("/api/private/setup", json={"password": "newpw"})
        self.assertEqual(r.status_code, 400)
        r = client_app.post("/api/private/setup",
                            json={"password": "newpw", "old_password": "abcd"})
        self.assertEqual(r.status_code, 200)
        client_app.post("/api/private/lock")
        r = client_app.post("/api/private/unlock", json={"password": "newpw"})
        self.assertEqual(r.status_code, 200)
        client_app.post("/api/private/lock")

    def test_short_password_rejected(self):
        r = client_app.post("/api/private/setup", json={"password": "abc"})
        self.assertEqual(r.status_code, 400)


class TestNsfwFiltering(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.set_setting("private_pw", "")   # 复位密码(前一类设置过)
        db.set_setting("private_enabled", "0")

    def test_workflow_hidden_until_unlocked(self):
        cur = db.execute(
            "INSERT INTO workflows(name, enabled, nsfw, template_json) VALUES(?,?,?,?)",
            ("私密工作流", 1, 1, json.dumps({"params": [], "batch_node": None, "workflow": ""})))
        wid = cur.lastrowid
        try:
            html = client_app.get("/workflows").get_data(as_text=True)
            self.assertNotIn("私密工作流", html)
            gen = client_app.get("/").get_data(as_text=True)
            self.assertNotIn("私密工作流", gen)
            self.assertEqual(client_app.get(f"/api/workflows/{wid}").status_code, 404)
            r = client_app.post("/api/private/unlock", json={"password": "pw1234"})
            self.assertEqual(r.status_code, 400)   # 未设密码拒绝
            db.set_setting("private_pw", "salt$deadbeef")   # 直接造一个(解锁仍会失败)
            db.set_setting("private_enabled", "1")          # 模拟已开启
            html = client_app.get("/workflows").get_data(as_text=True)
            self.assertIn("私密工作流", html)
            self.assertEqual(client_app.get(f"/api/workflows/{wid}").status_code, 200)
        finally:
            db.execute("DELETE FROM workflows WHERE id=?", (wid,))
            db.set_setting("private_pw", "")
            db.set_setting("private_enabled", "0")

    def test_image_mark_private_moves_and_hides(self):
        rid = _place("nsfwmark_001_.png")
        row = library.indexed("nsfwmark_001_.png")
        self.assertFalse(row["nsfw"])
        self.assertFalse(row["path"].startswith("library/nsfw/"))
        try:
            r = client_app.post(f"/api/library/image/{rid}/nsfw", json={"nsfw": True})
            self.assertEqual(r.status_code, 200)
            row = library.indexed("nsfwmark_001_.png")
            self.assertTrue(row["nsfw"])
            self.assertTrue(row["path"].startswith("library/nsfw/"))
            self.assertTrue((library.nas_root() / row["path"]).exists())
            # 锁定时画廊/详情/媒体全部隐藏
            html = client_app.get("/gallery").get_data(as_text=True)
            self.assertNotIn("nsfwmark", html)
            self.assertEqual(
                client_app.get(f"/libmedia/{rid}").status_code, 404)
            self.assertEqual(
                client_app.get(f"/gallery/view/{rid}").status_code, 404)
            # 开启后可见
            db.set_setting("private_enabled", "1")
            html = client_app.get("/gallery").get_data(as_text=True)
            self.assertIn("nsfwmark", html)
            self.assertEqual(client_app.get(f"/libmedia/{rid}").status_code, 200)
            # 取消私密:移回普通区
            r = client_app.post(f"/api/library/image/{rid}/nsfw", json={"nsfw": False})
            self.assertEqual(r.status_code, 200)
            row = library.indexed("nsfwmark_001_.png")
            self.assertFalse(row["nsfw"])
            self.assertFalse(row["path"].startswith("library/nsfw/"))
        finally:
            with library._lock:
                conn = library._lib_connect()
                conn.execute("DELETE FROM files WHERE filename='nsfwmark_001_.png'")
                conn.commit()
            db.set_setting("private_enabled", "0")

    def test_workflow_mark_migrates_history(self):
        """标工作流私密:其历史图片自动移入 nsfw/,别的工作流图片不受影响。"""
        import library
        wf_a = db.execute(
            "INSERT INTO workflows(name, enabled, template_json) VALUES('wf迁移A', 1, '{}')").lastrowid
        wf_b = db.execute(
            "INSERT INTO workflows(name, enabled, template_json) VALUES('wf迁移B', 1, '{}')").lastrowid
        fns = []
        for fn in ("wfmig_a1_.png", "wfmig_a2_.png", "wfmig_b1_.png"):
            _place(fn)
            fns.append(fn)
            owner = wf_a if "a" in fn else wf_b
            tid = db.execute(
                "INSERT INTO tasks(workflow_id, workflow_name, status) VALUES(?, 't', 'done')",
                (owner,)).lastrowid
            db.execute("INSERT INTO images(task_id, filename, subfolder, type) VALUES(?,?, '', 'output')",
                       (tid, fn))
        cands = library.private_candidates_by_workflow(wf_a)
        self.assertEqual(sorted(cands),
                         sorted([library.indexed(fns[0])["rowid"],
                                 library.indexed(fns[1])["rowid"]]))
        library.migrate_to_private(cands)
        self.assertTrue(library.indexed(fns[0])["path"].startswith("library/nsfw/"))
        self.assertTrue(library.indexed(fns[1])["path"].startswith("library/nsfw/"))
        self.assertFalse(library.indexed(fns[2])["path"].startswith("library/nsfw/"))
        # 清理:files 在 library.db;images/tasks/workflows 在 comfyweb.db
        with library._lock:
            conn = library._lib_connect()
            for fn in fns:
                conn.execute("DELETE FROM files WHERE filename=?", (fn,))
            conn.commit()
        for wid in (wf_a, wf_b):
            for t in db.query("SELECT id FROM tasks WHERE workflow_id=?", (wid,)):
                db.execute("DELETE FROM images WHERE task_id=?", (t["id"],))
            db.execute("DELETE FROM tasks WHERE workflow_id=?", (wid,))
            db.execute("DELETE FROM workflows WHERE id=?", (wid,))

    def test_batch_endpoint(self):
        """批量端点:私密化/删除/参数校验/上限。"""
        rids = {fn: _place(fn) for fn in ("bat1_.png", "bat2_.png", "bat3_.png")}
        try:
            r = client_app.post("/api/gallery/batch",
                                json={"action": "private", "rowids": [rids["bat1_.png"], rids["bat2_.png"]],
                                      "nsfw": True})
            self.assertEqual(r.get_json()["done"], 2)
            self.assertTrue(library.indexed("bat1_.png")["nsfw"])
            r = client_app.post("/api/gallery/batch",
                                json={"action": "delete", "rowids": [rids["bat2_.png"], rids["bat3_.png"]]})
            self.assertEqual(r.get_json()["done"], 2)
            self.assertIsNone(library.indexed("bat2_.png"))
            self.assertIsNone(library.indexed("bat3_.png"))
            self.assertEqual(client_app.post("/api/gallery/batch",
                           json={"action": "x", "rowids": [1]}).status_code, 400)
            self.assertEqual(client_app.post("/api/gallery/batch",
                           json={"action": "delete", "rowids": list(range(501))}).status_code, 400)
        finally:
            with library._lock:
                conn = library._lib_connect()
                for fn in ("bat1_.png", "bat2_.png", "bat3_.png"):
                    conn.execute("DELETE FROM files WHERE filename=?", (fn,))
                conn.commit()

    def test_model_meta_nsfw_endpoint(self):
        r = client_app.post("/api/local/meta-nsfw",
                            json={"folder": "checkpoints", "filename": "x.safetensors",
                                  "nsfw": True})
        self.assertEqual(r.status_code, 200)
        row = db.query_one(
            "SELECT nsfw FROM model_meta WHERE folder='checkpoints' AND filename='x.safetensors'")
        self.assertEqual(row["nsfw"], 1)
        db.execute("DELETE FROM model_meta WHERE folder='checkpoints' AND filename='x.safetensors'")


if __name__ == "__main__":
    unittest.main()
