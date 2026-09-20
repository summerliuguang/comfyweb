"""设置页、ComfyUI 状态、队列管理、主机监控与健康检查。"""
import hashlib
import time

from flask import Blueprint, render_template, request

import db
from comfy_client import ComfyError, client

from views.helpers import (TOGGLE_FEATURES, clear_cached, err, feature_cache_invalidate, feature_enabled)

bp = Blueprint("admin", __name__)


@bp.get("/settings")
def page_settings():
    token = db.get_setting("civitai_token")
    return render_template("settings.html", comfy_url=db.get_setting("comfy_url"),
                           civitai_proxy=db.get_setting("civitai_proxy"),
                           civitai_token_set=bool(token),
                           civitai_nsfw=db.get_setting("civitai_nsfw") == "1",
                           active="settings")


# ---------- 设置 / 状态 API ----------

@bp.post("/api/settings")
def api_save_settings():
    data = request.get_json(silent=True) or {}
    url = (data.get("comfy_url") or "").strip().rstrip("/")
    if "comfy_url" in data:
        db.set_setting("comfy_url", url)
    if "civitai_proxy" in data:
        db.set_setting("civitai_proxy", (data.get("civitai_proxy") or "").strip())
    if "civitai_token" in data:
        # 留空表示保持不变,避免 Token 明文回显到页面
        tok = (data.get("civitai_token") or "").strip()
        if tok:
            db.set_setting("civitai_token", tok)
    if data.get("civitai_token_clear"):
        db.set_setting("civitai_token", "")
    if "civitai_nsfw" in data:
        db.set_setting("civitai_nsfw", "1" if data.get("civitai_nsfw") else "0")
    try:  # civitai 模块被禁用/损坏时不影响设置保存
        import civitai
        civitai.reset_session()  # 代理/Token 可能已变更,丢弃旧连接
    except ImportError:
        pass
    if "record_keep" in data:
        try:
            keep = max(20, min(5000, int(data.get("record_keep") or 200)))
        except (TypeError, ValueError):
            keep = 200
        db.set_setting("record_keep", str(keep))
    if "library_dir" in data:
        raw = (data.get("library_dir") or "").strip()
        if raw:
            import os as _os
            p = _os.path.expanduser(raw)
            if not _os.path.isabs(raw):
                return err("整理库目录必须是绝对路径(留空则用默认 servershare/comfyui/library)")
            try:
                _os.makedirs(p, exist_ok=True)
            except OSError as e:
                return err(f"目录不可创建: {e}")
            db.set_setting("library_dir", raw)
        else:
            db.set_setting("library_dir", "")  # 空 = 默认 ~/servershare/comfyui/library
        try:
            import storage
            storage.trigger_sync()
        except ImportError:
            pass
    try:
        import civitai
        civitai.clear_cache()
    except ImportError:
        pass
    return {"ok": True}


@bp.post("/api/settings/test")
def api_test_connection():
    data = request.get_json(silent=True) or {}
    url = (data.get("comfy_url") or "").strip()
    if url:
        db.set_setting("comfy_url", url.rstrip("/"))
    try:
        stats = client.system_stats()
    except ComfyError as e:
        return err(e, 502)
    sysinfo = stats.get("system") or {}
    devices = stats.get("devices") or []
    dev = devices[0] if devices else {}
    vram_free = dev.get("vram_free") or 0
    vram_total = dev.get("vram_total") or 0
    return {
        "ok": True,
        "version": sysinfo.get("comfyui_version") or "未知",
        "python": sysinfo.get("python_version") or "",
        "device": dev.get("name") or "未知设备",
        "vram": f"{vram_free / 1024**3:.1f} / {vram_total / 1024**3:.1f} GB",
        "queue_remaining": None,
    }


@bp.get("/api/status")
def api_status():
    return {"ws": client.ws_state, "last_event": client.last_event_ts}


@bp.post("/api/reconnect")
def api_reconnect():
    """手动重连 ComfyUI(WS 连续失败达上限后由用户触发)。"""
    client.reset_reconnect()
    return {"ok": True, "ws": client.ws_state}


# ---------- 队列(设置页) ----------

@bp.get("/api/queue")
def api_queue():
    try:
        q = client.queue()
    except ComfyError as e:
        return err(e, 502)
    ours = {r["prompt_id"]: r["id"] for r in
            db.query("SELECT id, prompt_id FROM tasks WHERE status IN ('queued','running')")}

    def brief(items):
        out = []
        for it in items or []:
            pid = it[1] if isinstance(it, list) and len(it) > 1 else None
            out.append({"prompt_id": pid, "task_id": ours.get(pid)})
        return out
    return {"queue_running": brief(q.get("queue_running")),
            "queue_pending": brief(q.get("queue_pending"))}


@bp.post("/api/queue/clear")
def api_queue_clear():
    try:
        client.queue_clear()
    except ComfyError as e:
        return err(e, 502)
    return {"ok": True}


@bp.post("/api/cache/clear")
def api_cache_clear():
    """清空模型列表缓存(在 ComfyUI 加了新模型后手动刷新)。"""
    clear_cached("models:")
    return {"ok": True}


@bp.post("/api/queue/interrupt")
def api_queue_interrupt():
    try:
        client.interrupt()
    except ComfyError as e:
        return err(e, 502)
    return {"ok": True}


@bp.get("/api/features")
def api_features_get():
    """功能开关当前状态(设置页展示)。"""
    return {"features": {n: feature_enabled(n) for n in TOGGLE_FEATURES}}


@bp.post("/api/features")
def api_features_set():
    """改功能开关:存数据库即时生效(优先于 .env 的 ENABLE_* 初始默认)。"""
    d = request.get_json(silent=True) or {}
    name = d.get("feature") or ""
    if name not in TOGGLE_FEATURES:
        return err("无效的功能名")
    enabled = bool(d.get("enabled"))
    db.set_setting("enable_" + name, "1" if enabled else "0")
    feature_cache_invalidate()   # 绕过 3s 缓存,开关即时生效
    return {"ok": True, "feature": name, "enabled": enabled}


# ---------- 私密内容(NSFW)密码与开关 ----------

_PW_ITER = 120000
_pw_fail = {"n": 0, "until": 0.0}   # 简单防爆破:5 次失败锁 60 秒(内网+basic auth 之上再加一层)


def _pw_hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _PW_ITER).hex()


@bp.get("/api/private/status")
def api_private_status():
    return {"configured": bool(db.get_setting("private_pw")),
            "enabled": (db.get_setting("private_enabled") or "") == "1"}


@bp.post("/api/private/setup")
def api_private_setup():
    """首次设置密码;已设置时需验证旧密码才能改。"""
    import secrets
    d = request.get_json(silent=True) or {}
    password = (d.get("password") or "").strip()
    if len(password) < 4:
        return err("密码至少 4 位")
    old_hash = db.get_setting("private_pw")
    if old_hash:
        if _pw_fail["n"] >= 5 and time.time() < _pw_fail["until"]:
            return err("失败次数过多,请 1 分钟后再试")
        old = (d.get("old_password") or "").strip()
        salt, _, want = old_hash.partition("$")
        if _pw_hash(old, salt) != want:
            _pw_fail["n"] += 1
            _pw_fail["until"] = time.time() + 60
            return err("旧密码不对")
    salt = secrets.token_hex(16)
    db.set_setting("private_pw", f"{salt}${_pw_hash(password, salt)}")
    _pw_fail.update(n=0, until=0.0)
    return {"ok": True}


@bp.post("/api/private/unlock")
def api_private_unlock():
    d = request.get_json(silent=True) or {}
    password = (d.get("password") or "").strip()
    saved = db.get_setting("private_pw")
    if not saved:
        return err("尚未设置密码,请先在下方设置")
    if _pw_fail["n"] >= 5 and time.time() < _pw_fail["until"]:
        return err("失败次数过多,请 1 分钟后再试")
    salt, _, want = saved.partition("$")
    if _pw_hash(password, salt) != want:
        _pw_fail["n"] += 1
        _pw_fail["until"] = time.time() + 60
        return err("密码不对")
    _pw_fail.update(n=0, until=0.0)
    db.set_setting("private_enabled", "1")
    return {"ok": True, "enabled": True}


@bp.post("/api/private/lock")
def api_private_lock():
    """上锁(隐藏私密内容)免密——关闭只是隐藏,不构成泄露。"""
    db.set_setting("private_enabled", "0")
    return {"ok": True, "enabled": False}


@bp.get("/api/storage")
def api_storage_status():
    """图片存储:配置目录/挂载/降级状态/本地缓存数/待同步数。"""
    import storage
    return storage.status()


@bp.post("/api/storage/sync")
def api_storage_sync():
    """立即触发一轮补拉扫描与本地→存储目录同步。"""
    import storage
    storage.trigger_sync()
    return {"ok": True}


@bp.get("/api/host/stats")
def api_host_stats():
    """ComfyUI 主机的 GPU/系统状态(跨机:显存走 ComfyUI 接口,温度/利用率需 SSH)。"""
    import hoststats
    return hoststats.fetch()


@bp.get("/healthz")
def healthz():
    return "ok"


def register(app):
    app.register_blueprint(bp)
