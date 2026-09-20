"""模型/LoRA 页:Civitai 搜索与详情、本机已装模型识别、CDN 封面图代理。"""
import json
import logging
import re
import threading
import time

from flask import Blueprint, Response, jsonify, render_template, request

import civitai
import db
from comfy_client import ComfyError, client

from views.helpers import cached, err, img_cache_get, img_cache_store

bp = Blueprint("civitai", __name__)

LOCAL_MODEL_DIRS = {"checkpoints": "checkpoints", "loras": "loras",
                    "diffusion_models": "diffusion_models", "vae": "vae"}
FOLDER_CIV_TYPE = {"checkpoints": "Checkpoint", "diffusion_models": "Checkpoint",
                   "loras": "LORA", "vae": "VAE"}

CIVITAI_TYPES = {"Checkpoint", "LORA"}


@bp.get("/models")
def page_models():
    return render_template("civitai.html", civ_type="Checkpoint", title="模型",
                           active="models")


@bp.get("/loras")
def page_loras():
    return render_template("civitai.html", civ_type="LORA", title="LoRA",
                           active="loras")


@bp.get("/api/civitai/search")
def api_civitai_search():
    q = request.args.get("q", "").strip()
    ctype = request.args.get("type", "Checkpoint")
    if ctype not in CIVITAI_TYPES:
        return err("无效的类型")
    try:
        return jsonify(civitai.search_local(
            q=q or None, types=(ctype,), base=request.args.get("base") or None,
            sort=request.args.get("sort") or "Most Downloaded",
            cursor=request.args.get("cursor") or None,
            nsfw=db.get_setting("civitai_nsfw") == "1",
            refresh=request.args.get("refresh") == "1"))
    except civitai.CivitaiError as e:
        return err(e, 502)


@bp.get("/api/civitai/model/<int:mid>")
def api_civitai_model(mid):
    try:
        return jsonify(civitai.get_model_local(
            mid, refresh=request.args.get("refresh") == "1"))
    except civitai.CivitaiError as e:
        return err(e, 502)


@bp.get("/api/local/models")
def api_local_models():
    """本机(ComfyUI 侧)已安装的模型文件列表,带 Civitai 匹配元数据。"""
    folder = request.args.get("type", "checkpoints")
    if folder not in LOCAL_MODEL_DIRS:
        return err("无效的类型")
    try:
        files = cached("models:" + folder, 120, lambda f=folder: client.models(f))
    except ComfyError as e:
        return err(e, 502)
    metas = {r["filename"]: r for r in db.query("SELECT * FROM model_meta WHERE folder=?", (folder,))}
    from views.helpers import private_open
    open_ = private_open()
    out = []
    for f in sorted(str(f) for f in files):
        m = metas.get(f)
        if m and m["nsfw"] and not open_:
            continue   # 私密模式关闭:隐藏标记为私密的模型/LoRA
        out.append({
            "filename": f,
            "nsfw": bool(m and m["nsfw"]),
            "civ_id": m["civ_id"] if m else None,
            "civ_name": m["civ_name"] if m else "",
            "base_model": m["base_model"] if m else "",
            "trained_words": json.loads(m["trained_words"]) if m and m["trained_words"] else [],
            "cover": m["cover"] if m else "",
            "identified": bool(m),
        })
    return {"files": out}


_identify_lock = threading.Lock()
_identify_threads = {}


def identify_local_bg(folder, ctype):
    """后台逐个匹配未识别的本地模型(每文件间隔 1 秒,防 Civitai 限流)。"""
    try:
        files = cached("models:" + folder, 120, lambda f=folder: client.models(f))
        for f in sorted(str(x) for x in files):
            if db.query_one("SELECT 1 FROM model_meta WHERE folder=? AND filename=?", (folder, f)):
                continue
            stem = re.sub(r"\.(safetensors|ckpt|pt|sft)$", "", f, flags=re.I)
            try:
                meta = civitai.match_by_filename(stem, ctype)
            except civitai.CivitaiError:
                return  # 网络/代理不可用,下次触发再试
            db.execute(
                "INSERT OR REPLACE INTO model_meta(folder, filename, civ_id, civ_name, "
                "base_model, trained_words, cover) VALUES(?,?,?,?,?,?,?)",
                (folder, f, meta["civ_id"] if meta else None,
                 meta["civ_name"] if meta else "", meta["base_model"] if meta else "",
                 json.dumps(meta["trained_words"], ensure_ascii=False) if meta else "[]",
                 meta["cover"] if meta else ""))
            time.sleep(1.0)
    except Exception:
        logging.getLogger("comfyweb").exception(
            "后台识别本地模型异常(folder=%s)", folder)
    finally:
        with _identify_lock:
            _identify_threads.pop(folder, None)


@bp.post("/api/local/meta-nsfw")
def api_local_meta_nsfw():
    """标记/取消模型或 LoRA 的私密(无识别记录的也可标记)。"""
    d = request.get_json(silent=True) or {}
    folder, filename = d.get("folder") or "", d.get("filename") or ""
    if folder not in LOCAL_MODEL_DIRS or not filename:
        return err("参数无效")
    nsfw = 1 if d.get("nsfw") else 0
    if db.query_one("SELECT 1 FROM model_meta WHERE folder=? AND filename=?", (folder, filename)):
        db.execute("UPDATE model_meta SET nsfw=? WHERE folder=? AND filename=?",
                   (nsfw, folder, filename))
    else:
        db.execute("INSERT INTO model_meta(folder, filename, nsfw) VALUES(?,?,?)",
                   (folder, filename, nsfw))
    return {"ok": True, "nsfw": bool(nsfw)}


@bp.post("/api/local/identify")
def api_local_identify():
    """开始/继续后台识别指定目录的本地模型。"""
    data = request.get_json(silent=True) or {}
    folder = data.get("folder", "checkpoints")
    if folder not in LOCAL_MODEL_DIRS:
        return err("无效的类型")
    with _identify_lock:
        if folder in _identify_threads and _identify_threads[folder].is_alive():
            return {"ok": True, "running": True}
        t = threading.Thread(target=identify_local_bg,
                             args=(folder, FOLDER_CIV_TYPE.get(folder, "Checkpoint")),
                             daemon=True)
        _identify_threads[folder] = t
        t.start()
    return {"ok": True, "running": True}


@bp.post("/api/local/identify/clear")
def api_local_identify_clear():
    data = request.get_json(silent=True) or {}
    folder = data.get("folder", "loras")
    if folder not in LOCAL_MODEL_DIRS:
        return err("无效的类型")
    db.execute("DELETE FROM model_meta WHERE folder=?", (folder,))
    return {"ok": True}


@bp.get("/civimg")
def civitai_image_proxy():
    """civitai CDN 图片代理(局域网设备一般无法直连 civitai),封面落盘缓存。"""
    u = request.args.get("u", "")
    ck = "civ:" + u
    cached_path, cached_ct = img_cache_get(ck)
    if cached_path:
        try:
            return Response(cached_path.read_bytes(),
                            content_type=cached_ct or "image/jpeg",
                            headers={"Cache-Control": "public, max-age=604800"})
        except OSError:
            pass  # 缓存被并发淘汰:视为未命中,走在线拉取
    try:
        r = civitai.fetch_image(u)
    except civitai.CivitaiError as e:
        return err(e, 502)
    body = r.content
    r.close()
    ctype = r.headers.get("Content-Type", "image/jpeg")
    img_cache_store(ck, body, ctype)
    return Response(body, content_type=ctype,
                    headers={"Cache-Control": "public, max-age=604800"})


def register(app):
    app.register_blueprint(bp)


def warm():
    """WS 连上后预热各目录的模型文件列表(ComfyError 由预热调度方兜底)。"""
    for folder in LOCAL_MODEL_DIRS:
        try:
            cached("models:" + folder, 120, lambda f=folder: client.models(f))
        except ComfyError:
            continue
