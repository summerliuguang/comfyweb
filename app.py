"""ComfyWeb — ComfyUI 简易生成站(Flask 入口:环境加载、应用工厂、启动)。

路由按功能域拆在 views/ 包,每个模块可独立开关:
- 设置页「功能开关」即时切换(存数据库);
- ENABLE_* 环境变量作为初始默认;
- 模块导入失败自动跳过并记日志,不影响其他模块。
"""
import importlib
import logging
import os
import time
from urllib.parse import urlsplit

from flask import Flask, Response, request

import db
from comfy_client import client

from views.helpers import TOGGLE_FEATURES, err, feature_enabled


def _load_env_file():
    """轻量加载仓库根 .env(键不覆盖真实环境变量;.env 已 gitignore,不入库)。"""
    envf = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(envf):
        return
    with open(envf, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


_load_env_file()

# 功能模块:全部注册,请求时按开关门控;media(图片代理)与 admin 无开关,始终启用。
FEATURE_MODULES = [
    ("gen", "views.gen"),
    ("gallery", "views.gallery"),
    ("civitai", "views.civitai"),
    ("ai", "views.ai"),
    ("batch", "views.batch"),
    ("media", "views.media"),
    ("admin", "views.admin"),
]


def create_app():
    app = Flask(__name__)
    # 请求体上限:工作流导入 JSON 绰绰有余,同时挡住异常大包(nginx 层上限 20M)
    app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

    @app.before_request
    def same_origin_only():
        """控制接口只接受同源 POST(nginx 层另有 basic auth)。

        经 nginx 反代时 Host 可能不带端口(取决于 proxy_set_header),因此同时接受
        X-Forwarded-Port 指出的外部端口形式。
        """
        if request.method == "POST":
            origin = request.headers.get("Origin") or request.headers.get("Referer")
            if origin:
                netloc = urlsplit(origin).netloc
                allowed = {request.host}
                host_only = request.host.rsplit(":", 1)[0] if ":" in request.host else request.host
                fwd_port = request.headers.get("X-Forwarded-Port")
                if fwd_port:
                    allowed.add(f"{host_only}:{fwd_port}")
                if netloc and netloc not in allowed:
                    return err("拒绝跨源请求", 403)

    db.init_db()

    # 功能模块逐个注册:导入/注册失败只跳过该功能并记日志,不影响其他模块
    features, warm_hooks = {}, []
    for name, path in FEATURE_MODULES:
        try:
            mod = importlib.import_module(path)
            mod.register(app)
            features[name] = True
            if hasattr(mod, "warm"):
                warm_hooks.append(mod.warm)
        except Exception:
            features[name] = False
            logging.getLogger("comfyweb").exception(
                "功能模块 %s(%s) 加载失败,已跳过", name, path)
    app.config["FEATURES"] = features

    @app.before_request
    def feature_gate():
        """按功能开关门控请求:关闭模块的页面/API 直接 404,菜单同步隐藏。"""
        ep = request.endpoint or ""
        name = ep.split(".", 1)[0] if "." in ep else ""
        if name in TOGGLE_FEATURES and not feature_enabled(name):
            if request.path.startswith("/api/"):
                return err("该功能未启用,可在设置页开启", 404)
            return Response("该功能未启用,请到「设置」页开启。",
                            404, content_type="text/plain; charset=utf-8")

    @app.context_processor
    def inject_features():
        return {"features": {n: bool(features.get(n)) and feature_enabled(n)
                             for n in features}}

    client.ensure_ws()

    # WS 连上后预热各模块缓存(60s 节流;单个模块预热失败不影响其他)
    _last_warm = [0.0]

    def warm_caches():
        now = time.time()
        if now - _last_warm[0] < 60 or not db.get_setting("comfy_url"):
            return
        _last_warm[0] = now
        for hook in warm_hooks:
            try:
                hook()
            except Exception:
                logging.getLogger("comfyweb").exception(
                    "缓存预热失败(%s)", getattr(hook, "__module__", "?"))

    client.on_connect = warm_caches
    return app


app = create_app()

if __name__ == "__main__":
    # 默认只监听本机(正式部署经 nginx 反代);局域网直连时设 HOST=0.0.0.0
    app.run(host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", "5012")), threaded=True)
