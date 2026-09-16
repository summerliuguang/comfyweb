"""视图层共享工具:进程内缓存、功能开关、统一错误响应、图片代理 URL 与磁盘缓存。"""
import hashlib
import json
import os
import secrets
import threading
import time
from urllib.parse import urlencode

from flask import jsonify

import db

# 工作流模板参数定义允许携带的字段
PARAM_KEYS = {"name", "node_id", "input", "label", "widget", "visible", "advanced",
              "value", "min", "max", "step", "options", "dynamic", "role"}

# 功能开关:蓝图名 → 环境变量。数据库设置(enable_<名>,设置页写入)优先,
# 环境变量只是初始默认;未在设置页动过时完全跟随 .env。
FEATURE_FLAGS = {"gen": "ENABLE_GEN", "gallery": "ENABLE_GALLERY",
                 "civitai": "ENABLE_CIVITAI", "ai": "ENABLE_AI",
                 "batch": "ENABLE_BATCH"}
TOGGLE_FEATURES = tuple(FEATURE_FLAGS)


def feature_enabled(name):
    """功能是否启用:数据库设置优先,其次 ENABLE_* 环境变量,默认开。"""
    v = db.get_setting("enable_" + name)
    if v:
        return v == "1"
    flag = FEATURE_FLAGS.get(name)
    return not flag or os.environ.get(flag, "1").strip().lower() in ("1", "true", "on")

_cache = {}
_cache_lock = threading.Lock()


def cached(key, ttl, fn):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        if len(_cache) > 500:  # 防长驻进程内存缓慢增长:顺带清掉早已过期的项
            for k in [k for k, (ts, _) in _cache.items() if now - ts > 600]:
                _cache.pop(k, None)
    val = fn()
    with _cache_lock:
        _cache[key] = (now, val)
    return val


def clear_cached(prefix):
    """清掉指定前缀的缓存项(如 ComfyUI 加了新模型后清 models:)。"""
    with _cache_lock:
        for k in [k for k in _cache if k.startswith(prefix)]:
            _cache.pop(k, None)


def err(msg, code=400):
    return jsonify({"error": str(msg)}), code


def image_url(filename, subfolder, img_type, preview=None, dl=False):
    qs = urlencode({"filename": filename, "subfolder": subfolder or "",
                    "type": img_type or "output",
                    **({"preview": preview} if preview else {}),
                    **({"dl": 1} if dl else {})})
    return "/image?" + qs


# ---------- 图片磁盘缓存(缩略图/封面,内容不可变不做 TTL) ----------

IMG_CACHE = db.DATA_DIR / "cache" / "img"
IMG_CACHE_MAX_BYTES = 800 * 1024 * 1024  # 缓存目录上限,超过时淘汰最旧的 1/4


def _img_cache_paths(key):
    h = hashlib.sha1(key.encode()).hexdigest()
    return IMG_CACHE / h, IMG_CACHE / (h + ".json")


def img_cache_get(key):
    """命中返回 (文件路径, content_type)。"""
    p, m = _img_cache_paths(key)
    if p.exists() and m.exists():
        try:
            return p, json.loads(m.read_text()).get("ct", "image/jpeg")
        except (ValueError, OSError):
            return None, None
    return None, None


def img_cache_store(key, content: bytes, ctype: str):
    if len(content) > 30 * 1024 * 1024:
        return  # 超大图不入缓存
    try:
        IMG_CACHE.mkdir(parents=True, exist_ok=True)
        p, m = _img_cache_paths(key)
        p.write_bytes(content)
        m.write_text(json.dumps({"ct": ctype}))
        if secrets.randbelow(50) == 0:  # 约 2% 概率触发裁剪
            img_cache_prune()
    except OSError:
        pass


def img_cache_prune():
    files = [(f.stat().st_mtime, f) for f in IMG_CACHE.glob("*") if not f.name.endswith(".json")]
    total = sum(f.stat().st_size for _, f in files)
    if total <= IMG_CACHE_MAX_BYTES:
        return
    files.sort()
    for _, f in files[: max(1, len(files) // 4)]:
        f.unlink(missing_ok=True)
        (IMG_CACHE / (f.name + ".json")).unlink(missing_ok=True)
