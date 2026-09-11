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
        import app as app_mod
        from urllib.parse import quote

        class FakeResp:
            content = b"fake-jpeg-bytes"
            headers = {"Content-Type": "image/jpeg"}

            def close(self):
                pass

        calls = []
        orig = app_mod.civitai.fetch_image

        def fake_fetch(url):
            calls.append(url)
            return FakeResp()

        app_mod.civitai.fetch_image = fake_fetch
        try:
            u = quote("https://image.civitai.com/x/1.jpeg", safe="")
            r = self.c.get(f"/civimg?u={u}")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.data, b"fake-jpeg-bytes")
            # 同 URL 二次请求应命中缓存,不再回调 fetch_image
            app_mod.civitai.fetch_image = lambda url: (_ for _ in ()).throw(
                AssertionError("第二次请求不应回源"))
            r = self.c.get(f"/civimg?u={u}")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.data, b"fake-jpeg-bytes")
        finally:
            app_mod.civitai.fetch_image = orig

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
        import app as app_mod
        from comfy_client import client
        from workflow import WorkflowParseError  # noqa: F401  (确保 ComfyError 可用)
        from comfy_client import ComfyError
        orig_state, orig_fn = client.ws_state, app_mod.remote_workflow_names
        client.ws_state = "已连接"
        app_mod._udwf_fail_at[0] = 0.0

        def boom():
            raise ComfyError("连接 ComfyUI 失败: 测试")
        app_mod.remote_workflow_names = boom
        try:
            r = self.c.get("/api/remote/workflows")
            self.assertEqual(r.status_code, 502)
            r2 = self.c.get("/api/remote/workflows")
            self.assertEqual(r2.status_code, 503)
            self.assertIn("稍候", r2.get_json()["error"])
            app_mod._udwf_fail_at[0] = time.time() - 31  # 冷却期过后恢复
            r3 = self.c.get("/api/remote/workflows")
            self.assertEqual(r3.status_code, 502)
        finally:
            client.ws_state = orig_state
            app_mod.remote_workflow_names = orig_fn
            app_mod._udwf_fail_at[0] = 0.0


if __name__ == "__main__":
    unittest.main()
