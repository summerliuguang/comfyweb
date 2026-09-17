"""API 冒烟测试:进程内启动 mock ComfyUI,走 app 完整请求链。

使用独立临时数据目录(COMFYWEB_DATA_DIR),绝不读写生产 data/comfyweb.db。
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 必须在导入 app/db 之前设置,隔离测试数据
os.environ["COMFYWEB_DATA_DIR"] = tempfile.mkdtemp(prefix="comfyweb-test-")

import db  # noqa: E402
from tests import mock_comfy  # noqa: E402

# 简单 txt2img API 工作流
API_WF = {
    "3": {"class_type": "KSampler", "inputs": {
        "cfg": 7, "denoise": 1, "latent_image": ["5", 0], "model": ["4", 0],
        "negative": ["7", 0], "positive": ["6", 0], "sampler_name": "euler",
        "scheduler": "normal", "seed": 42, "steps": 4}},
    "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "v1-5-pruned.safetensors"}},
    "5": {"class_type": "EmptyLatentImage", "inputs": {"batch_size": 1, "height": 512, "width": 512}},
    "6": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["4", 1], "text": "masterpiece girl"}},
    "7": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["4", 1], "text": "bad hands"}},
    "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
    "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "t", "images": ["8", 0]}},
}


def setUpModule():
    mock_comfy.start(5099)
    time.sleep(1.2)
    db.init_db()
    db.set_setting("comfy_url", "http://127.0.0.1:5099")
    import app  # 延迟导入:确保上面已切换测试数据目录
    app.app.config["TESTING"] = True
    global client_app
    client_app = app.app.test_client()


class ApiSmoke(unittest.TestCase):
    def setUp(self):
        self.c = client_app
        # 同源钩子:测试请求默认无 Origin 头,不会被拦

    def test_full_generation_flow(self):
        # 连接测试
        r = self.c.post("/api/settings/test", json={})
        self.assertEqual(r.status_code, 200)
        # 解析
        r = self.c.post("/api/workflows/parse", json={"text": __import__("json").dumps(API_WF)})
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["node_count"], 7)
        self.assertEqual(data["batch_node"], "5")
        # 保存
        r = self.c.post("/api/workflows", json={
            "name": "冒烟模板", "workflow": API_WF, "params": data["params"],
            "batch_node": data["batch_node"]})
        self.assertEqual(r.status_code, 200)
        wid = r.get_json()["id"]
        # 模板读取:动态模型选项来自 mock
        g = self.c.get(f"/api/workflows/{wid}").get_json()
        ckpt = next(p for p in g["params"] if p["name"] == "4:ckpt_name")
        self.assertIn("dreamshaperXL.safetensors", ckpt["options"])
        # 生成 count=2(batch_size 路径),轮询到完成(mock 2 秒后出结果)
        r = self.c.post("/api/generate", json={
            "workflow_id": wid, "values": {"6:text": "a cat"}, "count": 2, "random_seed": True})
        self.assertEqual(r.status_code, 200)
        tid = r.get_json()["task_ids"][0]
        task = None
        for _ in range(20):
            time.sleep(1)
            task = self.c.get(f"/api/tasks?ids={tid}").get_json()["tasks"][0]
            if task["status"] not in ("queued", "running"):
                break
        self.assertEqual(task["status"], "done")
        self.assertEqual(len(task["images"]), 2)
        # 图片代理:原图、缩略图(走缓存)、下载头三种形态
        img_url = task["images"][0]["url"]
        r = self.c.get(img_url)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data.startswith(b"\x89PNG"))
        r = self.c.get(task["images"][0]["thumb"])
        self.assertEqual(r.status_code, 200)
        r2 = self.c.get(task["images"][0]["thumb"])
        self.assertEqual(r2.status_code, 200)  # 第二次命中磁盘缓存
        r = self.c.get(img_url + "&dl=1")
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r.headers.get("Content-Disposition", ""))
        # 画廊出现该任务图片
        gal = self.c.get("/gallery")
        self.assertEqual(gal.status_code, 200)
        self.assertIn("a cat", gal.get_data(as_text=True))
        # 清理
        self.c.post(f"/api/workflows/{wid}/delete")

    def test_cross_origin_post_blocked(self):
        r = self.c.post("/api/settings", json={}, headers={"Origin": "http://evil.example"})
        self.assertEqual(r.status_code, 403)
        # test_client 的 Host 是 localhost,同源 Origin 应放行
        r = self.c.post("/api/settings", json={}, headers={"Origin": "http://localhost"})
        self.assertEqual(r.status_code, 200)

    def test_civitai_image_proxy(self):
        """/civimg 走 fetch_image 的 requests 响应(.content),二次请求命中磁盘缓存。"""
        import civitai as civ_mod
        from urllib.parse import quote

        class FakeResp:
            content = b"fake-jpeg-bytes"
            headers = {"Content-Type": "image/jpeg"}

            def close(self):
                pass

        calls = []
        orig = civ_mod.fetch_image

        def fake_fetch(url):
            calls.append(url)
            return FakeResp()

        civ_mod.fetch_image = fake_fetch
        try:
            u = quote("https://image.civitai.com/x/1.jpeg", safe="")
            r = self.c.get(f"/civimg?u={u}")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.data, b"fake-jpeg-bytes")
            # 同 URL 二次请求应命中缓存,不再回调 fetch_image
            civ_mod.fetch_image = lambda url: (_ for _ in ()).throw(
                AssertionError("第二次请求不应回源"))
            r = self.c.get(f"/civimg?u={u}")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.data, b"fake-jpeg-bytes")
        finally:
            civ_mod.fetch_image = orig

    def test_remote_workflows_fast_fail_when_disconnected(self):
        """WS 明确未连接时 /api/remote/workflows 应快速 503,不进入 3 次重试的长等待。"""
        import app as app_mod
        from comfy_client import client
        orig = client.ws_state
        client.ws_state = "未启动"
        try:
            r = self.c.get("/api/remote/workflows")
            self.assertEqual(r.status_code, 503)
            self.assertIn("ComfyUI", r.get_json()["error"])
        finally:
            client.ws_state = orig

    def test_remote_workflows_failure_cooldown(self):
        """拉取失败后 30s 冷却期内直接 503,不再每次都跑满重试周期。"""
        import time
        from comfy_client import client
        from comfy_client import ComfyError
        from views import gen as vgen
        orig_state, orig_fn = client.ws_state, vgen.remote_workflow_names
        client.ws_state = "已连接"
        vgen._udwf_fail_at[0] = 0.0

        def boom():
            raise ComfyError("连接 ComfyUI 失败: 测试")
        vgen.remote_workflow_names = boom
        try:
            r = self.c.get("/api/remote/workflows")
            self.assertEqual(r.status_code, 502)
            r2 = self.c.get("/api/remote/workflows")
            self.assertEqual(r2.status_code, 503)
            self.assertIn("稍候", r2.get_json()["error"])
            vgen._udwf_fail_at[0] = time.time() - 31  # 冷却期过后恢复
            r3 = self.c.get("/api/remote/workflows")
            self.assertEqual(r3.status_code, 502)
        finally:
            client.ws_state = orig_state
            vgen.remote_workflow_names = orig_fn
            vgen._udwf_fail_at[0] = 0.0

    def test_parse_rejects_non_object_json(self):
        """粘贴 JSON 数组/数字应返回 400 友好报错,而不是 AttributeError 500。"""
        for text in ("[1,2,3]", "42", '"text"'):
            r = self.c.post("/api/workflows/parse", json={"text": text})
            self.assertEqual(r.status_code, 400)
            self.assertIn("不是有效的工作流", r.get_json()["error"])

    def test_refine_cast_count_capped(self):
        """套图模式每人展开 7 张,人数上限 8(共 56 张)才不超单批 60 张上限。"""
        import batchgen

        def fake_llm(messages, max_tokens=4000, model=None):
            return {"characters": [
                {"cn": f"角色{i}", "look": "girl", "outfit": "dress",
                 "scenes": [{"cn": f"场景{j}", "desc": f"scene {j}"}
                            for j in range(6)]}
                for i in range(12)]}

        tasks = batchgen.refine({"mode": "cast", "theme": "测试", "count": 12}, fake_llm)
        self.assertEqual(len(tasks), 56)  # 8 人 × 7 张,而非 12 人 × 7 = 84
        self.assertTrue(all(t["pipeline"] == "anima" for t in tasks))

    def test_batch_default_model_prefers_free(self):
        """批量细化默认模型应优先选免费模型(:free),没有免费的才退回网关默认。"""
        from views import ai as vai
        from views import batch as vbatch
        orig_fetch, orig_cached = vai._fetch_ai_models, vbatch.cached
        vai._fetch_ai_models = lambda b, k: [
            "deepseek-flash", "mimo-v2.5",
            "nvidia/nemotron-3-super-120b-a12b:free",
            "nvidia/nemotron-3-ultra-550b-a55b:free"]
        vbatch.cached = lambda key, ttl, fn: fn()  # 绕过缓存直接取
        try:
            self.assertEqual(vbatch._batch_default_model(),
                             "nvidia/nemotron-3-super-120b-a12b:free")
            vai._fetch_ai_models = lambda b, k: ["deepseek-flash", "mimo-v2.5"]
            self.assertEqual(vbatch._batch_default_model(), "deepseek-flash")
        finally:
            vai._fetch_ai_models, vbatch.cached = orig_fetch, orig_cached

    def test_feature_flags(self):
        """ENABLE_* 作为初始默认:置 0 后对应路由 404、菜单隐藏,其余模块不受影响。"""
        import os
        import app as app_mod
        os.environ["ENABLE_CIVITAI"] = "0"
        try:
            app2 = app_mod.create_app()
            self.assertTrue(app2.config["FEATURES"]["civitai"])  # 模块仍加载,由门控拦截
            c = app2.test_client()
            self.assertEqual(c.get("/models").status_code, 404)
            self.assertEqual(c.get("/api/civitai/search").status_code, 404)
            self.assertEqual(c.get("/").status_code, 200)  # 其余模块照常
            html = c.get("/settings").get_data(as_text=True)
            self.assertNotIn('href="/models"', html)  # 菜单入口已隐藏
        finally:
            os.environ.pop("ENABLE_CIVITAI", None)

    def test_feature_toggle_from_settings(self):
        """设置页开关(存数据库)即时生效:关闭即 404+菜单隐藏,重开恢复。"""
        import app as app_mod
        app2 = app_mod.create_app()
        c = app2.test_client()
        r = c.post("/api/features", json={"feature": "gallery", "enabled": False})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(c.get("/gallery").status_code, 404)
        self.assertNotIn('href="/gallery"', c.get("/settings").get_data(as_text=True))
        r = c.post("/api/features", json={"feature": "gallery", "enabled": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(c.get("/gallery").status_code, 200)
        self.assertIn('href="/gallery"', c.get("/settings").get_data(as_text=True))
        # 无效功能名被拒绝
        r = c.post("/api/features", json={"feature": "admin", "enabled": False})
        self.assertEqual(r.status_code, 400)
        db.set_setting("enable_gallery", "")  # 还原为跟随默认,避免影响其他用例

    def test_image_proxy_survives_gen_disabled(self):
        """/image 是画廊/收藏共用的基础设施:关闭 gen 后图片代理仍可用。"""
        import os
        import app as app_mod
        os.environ["ENABLE_GEN"] = "0"
        try:
            app2 = app_mod.create_app()
            c = app2.test_client()
            self.assertEqual(c.get("/").status_code, 404)  # gen 已关
            r = c.get("/image?filename=whatever.png")      # mock ComfyUI 对任意名回 PNG
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.data.startswith(b"\x89PNG"))
        finally:
            os.environ.pop("ENABLE_GEN", None)

    def test_prune_tasks(self):
        """记录清理:保留最近 record_keep 条,最旧的连图片记录一起删除。"""
        db.set_setting("record_keep", "20")
        try:
            ids = [db.execute(
                "INSERT INTO tasks(workflow_name) VALUES('prune-test')").lastrowid
                for _ in range(25)]
            img_id = db.execute(
                "INSERT INTO images(task_id, filename) VALUES(?, 'p.png')",
                (ids[0],)).lastrowid
            db.prune_tasks()
            left = {r["id"] for r in
                    db.query("SELECT id FROM tasks WHERE workflow_name='prune-test'")}
            self.assertEqual(len(left), 20)
            self.assertIn(ids[-1], left)       # 最新保留
            self.assertNotIn(ids[0], left)     # 最旧删除
            self.assertIsNone(db.query_one(
                "SELECT id FROM images WHERE id=?", (img_id,)))  # CASCADE 连带删
        finally:
            db.set_setting("record_keep", "")
            db.execute("DELETE FROM tasks WHERE workflow_name='prune-test'")

    def test_batch_status_reconciles_stale_tasks(self):
        """批量页轮询对活跃任务对账:history 缺失的超龄任务标 error 闭环,新任务不受影响。"""
        from comfy_client import client
        old_id = db.execute(
            "INSERT INTO tasks(prompt_id, workflow_name, status, created_at) "
            "VALUES('stale-pid', '批量生成', 'queued', "
            "datetime('now','localtime','-3 hours'))").lastrowid
        fresh_id = db.execute(
            "INSERT INTO tasks(prompt_id, workflow_name, status, created_at) "
            "VALUES('fresh-pid', '批量生成', 'queued', "
            "datetime('now','localtime'))").lastrowid
        orig = client.ws_state
        client.ws_state = "已断开,正在重连"  # 绕过 WS 新鲜度守卫,强制走对账
        try:
            self.c.get("/api/batch/status")
            old = db.query_one("SELECT status, error FROM tasks WHERE id=?", (old_id,))
            self.assertEqual(old["status"], "error")       # history 无记录 → 兜底闭环
            self.assertIn("丢失", old["error"] or "")
            fresh = db.query_one("SELECT status FROM tasks WHERE id=?", (fresh_id,))
            self.assertEqual(fresh["status"], "queued")    # 新任务不误杀
        finally:
            client.ws_state = orig
            db.execute("DELETE FROM tasks WHERE id IN (?,?)", (old_id, fresh_id))


class TestUrlValidation(unittest.TestCase):
    """comfy_url 校验:只放行解析到内网的 http(s) 纯主机地址(SSRF 防护边界)。"""

    def test_private_literal_ok(self):
        from comfy_client import ComfyClient
        ComfyClient._validate_url("http://192.168.1.10:8188")   # 不抛即通过
        ComfyClient._validate_url("https://10.0.0.2")
        ComfyClient._validate_url("http://[fd00::5]:8188")

    def _dns(self, ip):
        """临时把 comfy_client 的域名解析固定到指定 IP。"""
        import socket
        import comfy_client
        orig = comfy_client.socket.getaddrinfo

        def fake(host, port, **kw):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0))]
        comfy_client.socket.getaddrinfo = fake
        self.addCleanup(lambda: setattr(comfy_client.socket, "getaddrinfo", orig))

    def test_public_ip_rejected(self):
        from comfy_client import ComfyClient, ComfyError
        self._dns("93.184.216.34")
        with self.assertRaises(ComfyError):
            ComfyClient._validate_url("http://comfy.example.com:8188")

    def test_bad_shapes_rejected(self):
        from comfy_client import ComfyClient, ComfyError
        bad = [
            "ftp://192.168.1.5",                    # 非 http(s)
            "http://user:pass@192.168.1.5",         # 带凭据
            "http://192.168.1.5/api",               # 带路径
            "http://192.168.1.5?x=1",               # 带查询串
            "http://192.168.1.5\n:8188",            # 控制字符
            "http://not-a-host.example",            # 解析失败
        ]
        for url in bad:
            with self.assertRaises(ComfyError, msg=url):
                ComfyClient._validate_url(url)


if __name__ == "__main__":
    unittest.main()
