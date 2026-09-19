"""画廊筛选(类型/批次)与收藏功能的 API 冒烟测试。

直接经 db 模块落任务/图片种子数据,走 app 完整请求链;
数据目录沿用 test_api_smoke 的隔离设置(setdefault 保证单跑也可用)。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("COMFYWEB_DATA_DIR", tempfile.mkdtemp(prefix="comfyweb-test-"))

import db  # noqa: E402


def setUpModule():
    db.init_db()
    import app  # 延迟导入:确保上面已设置测试数据目录
    app.app.config["TESTING"] = True
    global client_app
    client_app = app.app.test_client()


def _seed_task(prompt, batch, category):
    """种一条任务+图片记录,并经 library.place 真实入索引(模拟归档线程完成后的状态)。"""
    cur = db.execute(
        "INSERT INTO tasks(workflow_name, prompt_text, model, batch, category, status) "
        "VALUES('批量生成', ?, 'm.safetensors', ?, ?, 'done')",
        (prompt, batch, category))
    tid = cur.lastrowid
    cur = db.execute(
        "INSERT INTO images(task_id, filename, subfolder, type) VALUES(?,?, '', 'output')",
        (tid, f"{prompt}.png"))
    f = Path(tempfile.mkdtemp()) / f"{prompt}.png"
    f.write_bytes(b"IMG")
    import library
    library.place(f, f"{prompt}.png")
    return cur.lastrowid


class BatchGalleryFilters(unittest.TestCase):
    def test_filter_by_category_and_batch(self):
        img_a = _seed_task("仙子立绘", "修仙批次", "立绘")
        img_b = _seed_task("丹炉特写", "修仙批次", "图标")
        img_c = _seed_task("坊市街道", "武侠批次", "背景")
        try:
            html_a = client_app.get("/gallery?cat=立绘").get_data(as_text=True)
            self.assertIn("仙子立绘", html_a)
            self.assertNotIn("丹炉特写", html_a)
            html_b = client_app.get("/gallery?batch=武侠批次").get_data(as_text=True)
            self.assertIn("坊市街道", html_b)
            self.assertNotIn("仙子立绘", html_b)
            html_c = client_app.get("/gallery?cat=图标&batch=修仙批次").get_data(as_text=True)
            self.assertIn("丹炉特写", html_c)
            self.assertNotIn("坊市街道", html_c)
            # 下拉选项来自已有数据
            plain = client_app.get("/gallery").get_data(as_text=True)
            self.assertIn("修仙批次", plain)
            self.assertIn("立绘", plain)
            # 详情参数接口带出类型与批次
            d = client_app.get(f"/api/gallery/image/{img_a}/params").get_json()
            self.assertEqual(d["category"], "立绘")
            self.assertEqual(d["batch"], "修仙批次")
        finally:
            import library
            for i in (img_a, img_b, img_c):
                db.execute("DELETE FROM images WHERE id=?", (i,))
                db.execute("DELETE FROM tasks WHERE prompt_text IN "
                           "('仙子立绘','丹炉特写','坊市街道')")
            for fn in ("仙子立绘.png", "丹炉特写.png", "坊市街道.png"):
                library._index_remove(fn)


class GalleryPagination(unittest.TestCase):
    def test_items_json_endpoint(self):
        """/api/gallery/items 分页 JSON(无限滚动数据源):条目、页码、总数。"""
        img = _seed_task("分页测试图", "分页批次", "立绘")
        try:
            d = client_app.get("/api/gallery/items?page=1").get_json()
            self.assertIn("items", d)
            self.assertGreaterEqual(d["total"], 1)
            self.assertGreaterEqual(d["pages"], 1)
            it = next(x for x in d["items"] if x["label"] == "分页测试图")
            self.assertIn("/libthumb/", it["thumb"])
            self.assertTrue(it["task_img_id"])
        finally:
            import library
            db.execute("DELETE FROM images WHERE id=?", (img,))
            db.execute("DELETE FROM tasks WHERE prompt_text='分页测试图'")
            library._index_remove("分页测试图.png")


class Favorites(unittest.TestCase):
    def test_fav_toggle_and_favorites_page(self):
        img = _seed_task("收藏测试图", "收藏批次", "立绘")
        try:
            # 收藏页初始为空
            page = client_app.get("/favorites").get_data(as_text=True)
            self.assertIn("还没有收藏", page)
            # 收藏
            r = client_app.post(f"/api/gallery/image/{img}/fav")
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.get_json()["fav"])
            # 画廊角标点亮 + 收藏页出现
            gal = client_app.get("/gallery").get_data(as_text=True)
            self.assertIn('<button class="fav-btn on"', gal)
            fav_page = client_app.get("/favorites").get_data(as_text=True)
            self.assertIn("收藏测试图", fav_page)
            self.assertIn('class="fav-btn on"', fav_page)
            # 详情页星标点亮
            detail = client_app.get(f"/gallery/image/{img}", follow_redirects=True).get_data(as_text=True)
            self.assertIn('id="btnFav"', detail)
            # 星标为内联 SVG(规范禁 emoji 字符),点亮态是填充色版本
            self.assertIn('id="btnFav"', detail)
            self.assertIn('fill="currentColor"', detail)
            # 再点一次取消
            r = client_app.post(f"/api/gallery/image/{img}/fav")
            self.assertFalse(r.get_json()["fav"])
            fav_page = client_app.get("/favorites").get_data(as_text=True)
            self.assertIn("还没有收藏", fav_page)
            # 不存在的图片返回 404
            r = client_app.post("/api/gallery/image/99999999/fav")
            self.assertEqual(r.status_code, 404)
        finally:
            import library
            db.execute("DELETE FROM images WHERE id=?", (img,))
            db.execute("DELETE FROM tasks WHERE prompt_text='收藏测试图'")
            library._index_remove("收藏测试图.png")


if __name__ == "__main__":
    unittest.main()
