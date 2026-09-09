"""ComfyWeb — ComfyUI 简易生成站(Flask 入口)。"""
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime
from urllib.parse import quote as urlquote, urlencode, urlsplit
from uuid import uuid4

from flask import (Flask, Response, jsonify, render_template, request)

import db
import civitai
import workflow as wfmod
from comfy_client import ComfyError, client

app = Flask(__name__)
PAGE_SIZE = 24
PARAM_KEYS = {"name", "node_id", "input", "label", "widget", "visible", "advanced",
              "value", "min", "max", "step", "options", "dynamic", "role"}


# ---------- 通用工具 ----------

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


def err(msg, code=400):
    return jsonify({"error": str(msg)}), code


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


# ---------- 工作流模板(存数据库;旧的 JSON 文件在启动时迁移入库) ----------

def load_tpl(row):
    tpl = db.get_workflow_template(row["id"])
    if tpl is None:
        raise ValueError(f"模板 {row['id']} 缺少内容")
    return tpl


def save_tpl(wid, data):
    db.set_workflow_template(int(wid), data)


def migrate_tpl_files():
    """旧版模板存 JSON 文件,启动时迁移入数据库。"""
    for row in db.query("SELECT id, filename, template_json FROM workflows"):
        if row["template_json"]:
            continue
        data = None
        try:
            p = db.WF_DIR / row["filename"]
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if data:
            db.set_workflow_template(row["id"], data)
            db.execute("UPDATE workflows SET filename=? WHERE id=?",
                       (f"wf_{row['id']}.json", row["id"]))


def sanitize_params(params):
    out, seen = [], set()
    for p in params or []:
        if not isinstance(p, dict) or ":" not in str(p.get("name", "")):
            continue
        q = {k: p[k] for k in PARAM_KEYS if k in p}
        if q["name"] in seen:
            continue
        seen.add(q["name"])
        out.append(q)
    return out


def fetch_object_info(classes):
    """按需取各节点类的 object_info,ComfyUI 不可达时返回 {}。"""
    info = {}
    for cls in classes:
        try:
            info.update(cached("oi:" + cls, 600, lambda c=cls: client.object_info(c)))
        except ComfyError:
            continue
    return info


def dynamic_options_for(param):
    """动态下拉选项:/models/<folder>,当前值不在列表时置顶。失败返回空。"""
    folder = param["dynamic"]
    try:
        opts = [str(o) for o in cached("models:" + folder, 120,
                                       lambda f=folder: client.models(f))]
    except ComfyError:
        return []
    val = str(param.get("value") or "")
    if val and val not in opts:
        opts.insert(0, val)
    return opts


def complete_select_options(tpl):
    """给模板里的 select 参数填充选项:动态模型列表 + 缺失的组合选项。"""
    classes = {}
    for p in tpl.get("params") or []:
        if p.get("widget") == "select" and not p.get("dynamic") and not p.get("options"):
            node = (tpl.get("workflow") or {}).get(p["node_id"]) or {}
            if node.get("class_type"):
                classes[p["node_id"]] = node["class_type"]
    oi = fetch_object_info(set(classes.values())) if classes else {}
    for p in tpl.get("params") or []:
        if p.get("widget") != "select":
            continue
        if p.get("dynamic"):
            p["options"] = dynamic_options_for(p)
        elif not p.get("options"):
            cls = classes.get(p["node_id"])
            if cls:
                p["options"] = wfmod.combo_options(oi, cls, p["input"])
            if not p.get("options"):
                p["options"] = []
    return tpl


def repair_template(tpl):
    """修复旧模板:补全空数值参数的默认值(取自 object_info)。

    Windows 版 ComfyUI 保存 UI JSON 时会把子目录路径写成双反斜杠、把未改动的
    数值控件存成空串;前者在导入时归一,这里处理后者。返回是否有修复。
    """
    wf = tpl.get("workflow") or {}
    classes = {}
    for p in tpl.get("params") or []:
        node = wf.get(p.get("node_id")) or {}
        cls = node.get("class_type")
        if not cls:
            continue
        empty_num = p.get("widget") in ("number", "float", "toggle") and p.get("value") in ("", None)
        if empty_num or (p.get("widget") == "select" and not p.get("dynamic")):
            classes[p["node_id"]] = cls
    if not classes:
        return False
    oi = fetch_object_info(set(classes.values()))
    fixed = False
    for p in tpl.get("params") or []:
        node = wf.get(p.get("node_id")) or {}
        cls = node.get("class_type") or classes.get(p["node_id"])
        if not cls:
            continue
        if p.get("widget") == "select" and not p.get("dynamic") and not p.get("options"):
            p["options"] = wfmod.combo_options(oi, cls, p["input"]) or []
        if p.get("widget") in ("number", "float", "toggle") and p.get("value") in ("", None):
            default = wfmod.input_default(oi, cls, p["input"])
            if default is not None:
                p["value"] = default
                node = wf.get(p["node_id"])
                if node is not None and p["input"] in (node.get("inputs") or {}):
                    node["inputs"][p["input"]] = default
                fixed = True
    return fixed


# ---------- 页面 ----------

@app.get("/")
def page_generate():
    rows = db.query("SELECT id, name FROM workflows WHERE enabled=1 ORDER BY id DESC")
    active = db.query(
        "SELECT id FROM tasks WHERE status IN ('queued','running') ORDER BY id DESC LIMIT 20")
    return render_template("generate.html", workflows=rows,
                           active_ids=[r["id"] for r in active], active="generate")


@app.get("/workflows")
def page_workflows():
    rows = db.query("SELECT * FROM workflows ORDER BY id DESC")
    return render_template("workflows.html", workflows=rows, active="workflows")


@app.get("/workflows/import")
def page_workflow_import():
    return render_template("workflow_import.html", active="workflows")


@app.get("/workflows/<int:wid>/edit")
def page_workflow_edit(wid):
    row = db.query_one("SELECT * FROM workflows WHERE id=?", (wid,))
    if not row:
        return "模板不存在", 404
    return render_template("workflow_edit.html", wf=row, active="workflows")


GALLERY_RANGES = {"today": "今天", "7d": "近 7 天", "30d": "近 30 天", "all": "全部"}


def _gallery_filter():
    """解析画廊筛选参数,返回 (where_sql, args, current)。"""
    conds, args, cur = [], [], {}
    q = request.args.get("q", "").strip()
    if q:
        conds.append("(t.prompt_text LIKE ? OR t.params_json LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    for key, col in (("wf", "t.workflow_name"), ("model", "t.model"), ("lora", "t.lora")):
        val = request.args.get(key, "").strip()
        if val:
            conds.append(f"{col} LIKE ?")
            args.append(f"%{val}%")
            cur[key] = val
    rng = request.args.get("range", "all")
    if rng in ("today", "7d", "30d"):
        span = {"today": "-1 day", "7d": "-7 days", "30d": "-30 days"}[rng]
        conds.append("t.created_at >= datetime('now','localtime',?)")
        args.append(span)
        cur["range"] = rng
    cur["q"] = q
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    return where, args, cur


@app.get("/gallery")
def page_gallery():
    where, args, cur = _gallery_filter()
    q = cur["q"]
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    total = db.query_one(
        f"SELECT COUNT(*) AS n FROM images i JOIN tasks t ON t.id=i.task_id {where}",
        args)["n"]
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, pages)
    rows = db.query(
        f"SELECT i.id, i.filename, i.subfolder, i.type, t.workflow_name, t.model, "
        f"t.prompt_text, t.created_at AS task_created, i.created_at AS img_created "
        f"FROM images i JOIN tasks t ON t.id=i.task_id {where} "
        f"ORDER BY i.id DESC LIMIT ? OFFSET ?",
        args + [PAGE_SIZE, (page - 1) * PAGE_SIZE])
    items = []
    for r in rows:
        it = dict(r)
        it["thumb"] = image_url(r["filename"], r["subfolder"], r["type"], preview="webp;jpeg;70")
        items.append(it)
    def short(s, limit=34):
        s = re.sub(r"^.*[\\/]", "", s or "")
        return s if len(s) <= limit else s[:limit - 1] + "…"

    opts = {
        "workflows": [r["workflow_name"] for r in db.query(
            "SELECT DISTINCT workflow_name FROM tasks WHERE workflow_name!='' ORDER BY 1")],
        "models": [(r["model"], short(r["model"])) for r in db.query(
            "SELECT DISTINCT model FROM tasks WHERE model!='' ORDER BY 1")],
        "loras": [(r["lora"], short(r["lora"])) for r in db.query(
            "SELECT DISTINCT lora FROM tasks WHERE lora!='' ORDER BY 1")],
    }
    filt = {k: v.strip() for k, v in request.args.items() if k != "page" and v.strip()}
    qs = urlencode(filt)
    return render_template("gallery.html", items=items, q=q, page=page, pages=pages,
                           total=total, opts=opts, cur=cur, ranges=GALLERY_RANGES,
                           qs=qs, active="gallery")


def _detail_qs():
    """详情页翻页要带上的筛选参数(不含 page)。"""
    return urlencode({k: v for k, v in request.args.items() if v.strip()})


@app.get("/api/gallery/image/<int:img_id>/params")
def api_gallery_image_params(img_id):
    """单图的完整参数(sheet 展开时按需拉取,切换图片后刷新)。"""
    row = db.query_one(
        "SELECT t.workflow_name, t.model, t.lora, t.prompt_text, t.seed, "
        "t.params_json, t.created_at AS task_created "
        "FROM images i JOIN tasks t ON t.id=i.task_id WHERE i.id=?", (img_id,))
    if not row:
        return err("图片不存在", 404)
    return {"workflow_name": row["workflow_name"],
            "model": row["model"],
            "lora": row["lora"],
            "prompt_text": row["prompt_text"],
            "seed": row["seed"],
            "created_at": row["task_created"],
            "params": json.loads(row["params_json"] or "[]")}


@app.get("/gallery/image/<int:img_id>")
def page_gallery_detail(img_id):
    where, args, _cur = _gallery_filter()
    row = db.query_one(
        "SELECT i.*, t.workflow_id, t.workflow_name, t.prompt_text, t.seed, t.params_json, "
        "t.model, t.lora, t.status, t.created_at AS task_created, t.count "
        "FROM images i JOIN tasks t ON t.id=i.task_id WHERE i.id=?", (img_id,))
    if not row:
        return "图片不存在(可能已被删除,或 ComfyUI 输出文件已清理)", 404
    prev_row = next_row = None
    if where:  # 带筛选时在筛选结果内翻页
        prev_row = db.query_one(
            f"SELECT i.id FROM images i JOIN tasks t ON t.id=i.task_id {where} "
            "AND i.id < ? ORDER BY i.id DESC LIMIT 1", args + [img_id])
        next_row = db.query_one(
            f"SELECT i.id FROM images i JOIN tasks t ON t.id=i.task_id {where} "
            "AND i.id > ? ORDER BY i.id LIMIT 1", args + [img_id])
    else:
        prev_row = db.query_one(
            "SELECT i.id FROM images i JOIN tasks t ON t.id=i.task_id "
            "WHERE i.id < ? ORDER BY i.id DESC LIMIT 1", (img_id,))
        next_row = db.query_one(
            "SELECT i.id FROM images i JOIN tasks t ON t.id=i.task_id "
            "WHERE i.id > ? ORDER BY i.id LIMIT 1", (img_id,))
    item = dict(row)
    item["params"] = json.loads(row["params_json"] or "[]")
    item["model_short"] = re.sub(r"^.*[\\/]", "", row["model"] or "") if row["model"] else ""
    item["url"] = image_url(row["filename"], row["subfolder"], row["type"])
    item["download"] = image_url(row["filename"], row["subfolder"], row["type"], dl=True)
    item["prev_id"] = prev_row["id"] if prev_row else None
    item["next_id"] = next_row["id"] if next_row else None
    pos_q = (f"SELECT COUNT(*) AS n FROM images i JOIN tasks t ON t.id=i.task_id {where} "
             f"AND i.id < ?" if where else
             "SELECT COUNT(*) AS n FROM images i JOIN tasks t ON t.id=i.task_id WHERE i.id < ?")
    pos_args = (args + [img_id]) if where else (img_id,)
    total_q = (f"SELECT COUNT(*) AS n FROM images i JOIN tasks t ON t.id=i.task_id {where}"
               if where else
               "SELECT COUNT(*) AS n FROM images i JOIN tasks t ON t.id=i.task_id")
    item["pos"] = db.query_one(pos_q, pos_args)["n"] + 1
    item["total"] = db.query_one(total_q, args if where else ())["n"]

    # 筛选集全量邻图(按时间序),前端做 AJAX 切换与静默预加载
    rows = db.query(
        f"SELECT i.id, i.filename, i.subfolder, i.type, t.workflow_id, t.workflow_name, "
        f"t.model, t.prompt_text, t.created_at AS task_created "
        f"FROM images i JOIN tasks t ON t.id=i.task_id {where} "
        f"ORDER BY i.id DESC LIMIT 400",
        args)
    neighbors, cur_idx = [], -1
    for r in rows:
        if r["id"] == img_id:
            cur_idx = len(neighbors)
        prompt = (r["prompt_text"] or "").strip()
        neighbors.append({
            "id": r["id"],
            "w": r["workflow_id"],
            "url": image_url(r["filename"], r["subfolder"], r["type"]),
            "thumb": image_url(r["filename"], r["subfolder"], r["type"], preview="webp;jpeg;70"),
            "prompt": prompt[:120],
            "wf": r["workflow_name"],
            "m": re.sub(r"^.*[\\/]", "", r["model"] or ""),
            "ts": r["task_created"],
        })
    neighbors, cur_idx = [], -1
    for r in rows:
        if r["id"] == img_id:
            cur_idx = len(neighbors)
        prompt = (r["prompt_text"] or "").strip()
        neighbors.append({
            "id": r["id"],
            "url": image_url(r["filename"], r["subfolder"], r["type"]),
            "thumb": image_url(r["filename"], r["subfolder"], r["type"], preview="webp;jpeg;70"),
            "prompt": prompt[:120],
            "wf": r["workflow_name"],
            "m": re.sub(r"^.*[\\/]", "", r["model"] or ""),
            "ts": r["task_created"],
        })
    return render_template("gallery_detail.html", item=item, qs=_detail_qs(),
                           neighbors=neighbors, idx=cur_idx, active="gallery")


@app.get("/models")
def page_models():
    return render_template("civitai.html", civ_type="Checkpoint", title="模型",
                           active="models")


@app.get("/loras")
def page_loras():
    return render_template("civitai.html", civ_type="LORA", title="LoRA",
                           active="loras")


# ---------- Civitai(模型/LoRA 信息) ----------

LOCAL_MODEL_DIRS = {"checkpoints": "checkpoints", "loras": "loras",
                    "diffusion_models": "diffusion_models", "vae": "vae"}
FOLDER_CIV_TYPE = {"checkpoints": "Checkpoint", "diffusion_models": "Checkpoint",
                   "loras": "LORA", "vae": "VAE"}

CIVITAI_TYPES = {"Checkpoint", "LORA"}


@app.get("/api/civitai/search")
def api_civitai_search():
    q = request.args.get("q", "").strip()
    ctype = request.args.get("type", "Checkpoint")
    if ctype not in CIVITAI_TYPES:
        return err("无效的类型")
    try:
        return jsonify(civitai.search(
            q=q or None, types=(ctype,), base=request.args.get("base") or None,
            sort=request.args.get("sort") or "Most Downloaded",
            cursor=request.args.get("cursor") or None,
            nsfw=db.get_setting("civitai_nsfw") == "1"))
    except civitai.CivitaiError as e:
        return err(e, 502)


@app.get("/api/civitai/model/<int:mid>")
def api_civitai_model(mid):
    try:
        return jsonify(civitai.get_model(mid))
    except civitai.CivitaiError as e:
        return err(e, 502)


@app.get("/api/local/models")
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
    out = []
    for f in sorted(str(f) for f in files):
        m = metas.get(f)
        out.append({
            "filename": f,
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
        pass
    finally:
        with _identify_lock:
            _identify_threads.pop(folder, None)


@app.post("/api/local/identify")
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


@app.post("/api/local/identify/clear")
def api_local_identify_clear():
    data = request.get_json(silent=True) or {}
    folder = data.get("folder", "loras")
    if folder not in LOCAL_MODEL_DIRS:
        return err("无效的类型")
    db.execute("DELETE FROM model_meta WHERE folder=?", (folder,))
    return {"ok": True}


@app.get("/api/prompts")
def api_prompt_history():
    """某模板最近用过的正面提示词(去重,新→旧)。"""
    try:
        wid = int(request.args.get("workflow_id", 0) or 0)
    except ValueError:
        wid = 0
    rows = db.query(
        "SELECT prompt_text, MIN(id) AS mid FROM tasks "
        "WHERE workflow_id=? AND prompt_text != '' GROUP BY prompt_text "
        "ORDER BY mid DESC LIMIT 8", (wid,))
    return {"prompts": [r["prompt_text"] for r in rows]}


@app.get("/civimg")
def civitai_image_proxy():
    """civitai CDN 图片代理(局域网设备一般无法直连 civitai),封面落盘缓存。"""
    u = request.args.get("u", "")
    ck = "civ:" + u
    cached_path, cached_ct = img_cache_get(ck)
    if cached_path:
        return Response(cached_path.read_bytes(),
                        content_type=cached_ct or "image/jpeg",
                        headers={"Cache-Control": "public, max-age=604800"})
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


@app.get("/settings")
def page_settings():
    token = db.get_setting("civitai_token")
    return render_template("settings.html", comfy_url=db.get_setting("comfy_url"),
                           civitai_proxy=db.get_setting("civitai_proxy"),
                           civitai_token_set=bool(token),
                           civitai_nsfw=db.get_setting("civitai_nsfw") == "1",
                           active="settings")


# ---------- 设置 / 状态 API ----------

@app.post("/api/settings")
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
    civitai.reset_session()  # 代理/Token 可能已变更,丢弃旧连接
    if "record_keep" in data:
        try:
            keep = max(20, min(5000, int(data.get("record_keep") or 200)))
        except (TypeError, ValueError):
            keep = 200
        db.set_setting("record_keep", str(keep))
    civitai.clear_cache()
    return {"ok": True}


@app.post("/api/settings/test")
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


@app.get("/api/status")
def api_status():
    return {"ws": client.ws_state, "last_event": client.last_event_ts}


# ---------- 工作流 API ----------

@app.post("/api/workflows/parse")
def api_workflow_parse():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return err("请粘贴工作流 JSON")
    try:
        wf = json.loads(text)
    except ValueError as e:
        return err(f"JSON 解析失败: {e}")
    classes = {v.get("class_type") for v in wf.values()
               if isinstance(v, dict) and v.get("class_type")}
    object_info = fetch_object_info(classes)
    try:
        tpl = wfmod.parse_workflow(wf, object_info)
    except wfmod.WorkflowParseError as e:
        return err(e)
    # 动态模型列表在导入评审时也解析出来,评审表格的默认值下拉才有完整选项
    for p in tpl["params"]:
        if p.get("widget") == "select" and p.get("dynamic"):
            p["options"] = dynamic_options_for(p)
    warnings = []
    if not object_info:
        warnings.append("未连接 ComfyUI,下拉选项将在连接后自动补全")
    return {"params": tpl["params"], "batch_node": tpl["batch_node"],
            "node_count": tpl["node_count"], "warnings": warnings}


@app.get("/api/remote/workflows")
def api_remote_workflows():
    """列出 ComfyUI 用户目录里已保存的工作流文件(预热后直接命中缓存)。"""
    try:
        names = remote_workflow_names()
    except ComfyError as e:
        return err(e, 502)
    names = sorted(str(n) for n in names if str(n).endswith(".json"))
    # 旧模板补来源:名字与远端文件名一致即视为从它导入(仅对 source 为空的行)
    names_set = set(names)
    for r in db.query("SELECT id, name FROM workflows WHERE source=''"):
        cand = f"{r['name']}.json"
        if r["name"] and cand in names_set:
            db.execute("UPDATE workflows SET source=? WHERE id=?", (cand, r["id"]))
    imported = db.query(
        "SELECT id, name, source, created_at FROM workflows WHERE source!='' ORDER BY id")
    return {"workflows": names, "imported": [dict(r) for r in imported]}


def _convert_remote(name):
    """拉取并转换服务器工作流,返回 (tpl, warnings)。文件名非法抛 WorkflowParseError。

    兼容两种存法:UI 格式(界面「保存」)与 API 格式(用户把「导出(API)」存进目录)。
    """
    if not name or "/" in name or "\\" in name or ".." in name or not name.endswith(".json"):
        raise wfmod.WorkflowParseError("无效的工作流文件名")
    uiwf = client.userdata_read("workflows/" + name)  # ComfyError 由调用方处理
    if not isinstance(uiwf, dict):
        raise wfmod.WorkflowParseError("工作流文件内容不是 JSON 对象")
    is_ui = isinstance(uiwf.get("nodes"), list)
    if is_ui:
        classes = {n.get("type") for n in uiwf["nodes"] if n.get("type")}
    else:
        classes = {v.get("class_type") for v in uiwf.values()
                   if isinstance(v, dict) and v.get("class_type")}
    object_info = fetch_object_info(classes)
    wf = wfmod.ui_to_api(uiwf, object_info) if is_ui else uiwf
    tpl = wfmod.parse_workflow(wf, object_info)
    for p in tpl["params"]:
        if p.get("widget") == "select" and p.get("dynamic"):
            p["options"] = dynamic_options_for(p)
    warnings = []
    missing = classes - set(object_info)
    if missing:
        warnings.append("以下节点类未取到定义(可能缺插件): " + ", ".join(sorted(missing)))
    return tpl, warnings


@app.post("/api/workflows/import_remote")
def api_workflow_import_remote():
    """拉取 ComfyUI 里已保存的工作流,UI 格式转 API 格式后走导入评审。"""
    name = ((request.get_json(silent=True) or {}).get("name") or "").strip()
    try:
        tpl, warnings = _convert_remote(name)
    except wfmod.WorkflowParseError as e:
        return err(e)
    except ComfyError as e:
        return err(e, 502)
    return {"workflow": tpl["workflow"], "params": tpl["params"], "batch_node": tpl["batch_node"],
            "node_count": tpl["node_count"], "warnings": warnings,
            "name": name[:-len(".json")]}


@app.post("/api/workflows/import_remote_save")
def api_workflow_import_remote_save():
    """一键导入:拉取转换后按默认参数定义直接保存为模板。"""
    name = ((request.get_json(silent=True) or {}).get("name") or "").strip()
    try:
        tpl, warnings = _convert_remote(name)
    except wfmod.WorkflowParseError as e:
        return err(e)
    except ComfyError as e:
        return err(e, 502)
    display = name[:-len(".json")] or "未命名"
    display = unique_workflow_name(display)
    cur = db.execute("INSERT INTO workflows(name) VALUES(?)", (display,))
    wid = cur.lastrowid
    db.execute("UPDATE workflows SET filename=?, source=? WHERE id=?",
               (f"wf_{wid}.json", name, wid))
    save_tpl(wid, {"version": 1, "name": display, "workflow": tpl["workflow"],
                   "params": tpl["params"], "batch_node": tpl["batch_node"]})
    return {"ok": True, "id": wid, "name": display, "warnings": warnings}


@app.post("/api/workflows")
def api_workflow_create():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip() or \
        datetime.now().strftime("工作流 %Y-%m-%d %H:%M")
    name = unique_workflow_name(name)
    wf = data.get("workflow")
    params = sanitize_params(data.get("params"))
    if not isinstance(wf, dict) or not wf:
        return err("缺少工作流 JSON")
    if not params:
        return err("没有可用的参数定义")
    cur = db.execute("INSERT INTO workflows(name) VALUES(?)", (name,))
    wid = cur.lastrowid
    source = (data.get("source") or "").strip()
    db.execute("UPDATE workflows SET filename=?, source=? WHERE id=?",
               (f"wf_{wid}.json", source, wid))
    save_tpl(wid, {"version": 1, "name": name, "workflow": wf,
                   "params": params, "batch_node": data.get("batch_node")})
    return {"ok": True, "id": wid}


@app.get("/api/workflows/<int:wid>")
def api_workflow_get(wid):
    row = db.query_one("SELECT * FROM workflows WHERE id=?", (wid,))
    if not row:
        return err("模板不存在", 404)
    tpl = complete_select_options(load_tpl(row))
    wfmod.sort_params(tpl["params"])
    if repair_template(tpl):
        db.set_workflow_template(wid, tpl)  # 修复结果落库,下次直接可用
    return {"id": row["id"], "name": row["name"], "enabled": row["enabled"],
            "params": tpl["params"], "batch_node": tpl["batch_node"],
            "workflow": tpl["workflow"]}


@app.post("/api/workflows/<int:wid>")
def api_workflow_update(wid):
    row = db.query_one("SELECT * FROM workflows WHERE id=?", (wid,))
    if not row:
        return err("模板不存在", 404)
    data = request.get_json(silent=True) or {}
    tpl = load_tpl(row)
    if "name" in data:
        tpl["name"] = (data.get("name") or "").strip() or tpl["name"]
    if "params" in data:
        params = sanitize_params(data.get("params"))
        if not params:
            return err("没有可用的参数定义")
        tpl["params"] = params
    save_tpl(wid, tpl)
    db.update_workflow_meta(wid, name=tpl["name"])
    if "enabled" in data:
        db.execute("UPDATE workflows SET enabled=? WHERE id=?",
                   (1 if data.get("enabled") else 0, int(wid)))
    return {"ok": True}


@app.post("/api/workflows/<int:wid>/delete")
def api_workflow_delete(wid):
    row = db.query_one("SELECT * FROM workflows WHERE id=?", (int(wid),))
    if not row:
        return err("模板不存在", 404)
    db.delete_workflow(int(wid))
    return {"ok": True}


# ---------- 生成 ----------

def prune_tasks():
    """按设置保留最近 N 条任务记录,更早的连图片记录一起清理。"""
    try:
        keep = max(20, int(db.get_setting("record_keep") or "200"))
    except ValueError:
        keep = 200
    row = db.query_one("SELECT id FROM tasks ORDER BY id DESC LIMIT 1 OFFSET ?",
                       (keep - 1,))
    if row:
        db.execute("DELETE FROM tasks WHERE id <= ?", (row["id"],))


def params_display(tpl, values, seed):
    items = []
    for p in tpl.get("params") or []:
        if not p.get("visible") or p.get("widget") == "seed":
            continue
        items.append({"label": p["label"],
                      "value": wfmod.coerce_value(p, values.get(p["name"], p.get("value")))})
    if seed is not None:
        items.append({"label": "随机种子", "value": seed})
    return items


def unique_workflow_name(base):
    """同名模板自动加 "(2)"/"(3)" 后缀。"""
    display, n = base, 2
    while db.query_one("SELECT 1 FROM workflows WHERE name=?", (display,)):
        display = f"{base} ({n})"
        n += 1
    return display


def extract_model_lora(tpl, values):
    """提取本次生成用的主模型与 LoRA(按动态下拉的目录判断),供画廊筛选。"""
    model, lora = "", ""
    for p in tpl.get("params") or []:
        val = str(values.get(p["name"], p.get("value")) or "").strip()
        if not val or val == "None":
            continue
        if p.get("dynamic") in ("checkpoints", "diffusion_models") and not model:
            model = val[:200]
        elif p.get("dynamic") == "loras":
            lora = f"{lora}, {val}" if lora else val
    return model, lora[:300]


@app.post("/api/generate")
def api_generate():
    data = request.get_json(silent=True) or {}
    row = db.query_one("SELECT * FROM workflows WHERE id=? AND enabled=1",
                       (data.get("workflow_id"),))
    if not row:
        return err("请先选择已启用的工作流模板")
    tpl = load_tpl(row)
    values = data.get("values") or {}
    try:
        count = max(1, min(int(data.get("count") or 1), 100))
    except (TypeError, ValueError):
        count = 1
    random_seed = bool(data.get("random_seed", True))
    use_batch = bool(tpl.get("batch_node"))
    task_ids, errors = [], []
    for _ in range(1 if use_batch else count):
        prompt, prompt_text, seed = wfmod.build_prompt(
            tpl, values, count=count if use_batch else None, random_seed=random_seed)
        try:
            prompt_id = client.submit(prompt)
        except ComfyError as e:
            cur = db.execute(
                "INSERT INTO tasks(workflow_id, workflow_name, prompt_text, seed, "
                "model, lora, params_json, status, error, count, finished_at) "
                "VALUES(?,?,?,?,?,?,?,?,'error',1,datetime('now','localtime'))",
                (row["id"], row["name"], prompt_text, seed,
                 *extract_model_lora(tpl, values),
                 json.dumps(params_display(tpl, values, seed), ensure_ascii=False), str(e)))
            task_ids.append(cur.lastrowid)
            errors.append(str(e))
            break
        cur = db.execute(
            "INSERT INTO tasks(prompt_id, workflow_id, workflow_name, prompt_text, seed, "
            "model, lora, params_json, status, count) VALUES(?,?,?,?,?,?,?,?,'queued',?)",
            (prompt_id, row["id"], row["name"], prompt_text, seed,
             *extract_model_lora(tpl, values),
             json.dumps(params_display(tpl, values, seed), ensure_ascii=False),
             count if use_batch else 1))
        task_ids.append(cur.lastrowid)
    client.ensure_ws()
    prune_tasks()
    return {"task_ids": task_ids, "error": "\n".join(errors)}


def image_url(filename, subfolder, img_type, preview=None, dl=False):
    qs = urlencode({"filename": filename, "subfolder": subfolder or "",
                    "type": img_type or "output",
                    **({"preview": preview} if preview else {}),
                    **({"dl": 1} if dl else {})})
    return "/image?" + qs


def serialize_task(t, images):
    return {
        "id": t["id"], "status": t["status"], "progress": t["progress"],
        "error": t["error"], "workflow_id": t["workflow_id"],
        "workflow_name": t["workflow_name"],
        "prompt_text": t["prompt_text"], "count": t["count"],
        "seed": t["seed"], "params": json.loads(t["params_json"] or "[]"),
        "created_at": t["created_at"],
        "images": [{"id": im["id"],
                    "thumb": image_url(im["filename"], im["subfolder"], im["type"],
                                       preview="webp;jpeg;75"),
                    "url": image_url(im["filename"], im["subfolder"], im["type"])}
                   for im in images],
    }


def reconcile_active_tasks(rows):
    """WS 掉线兜底:仅在 WS 断开或事件流停滞超过 10 秒时用 history 对账,
    WS 健康时每次轮询都查 history 会白白打 ComfyUI。"""
    if client.ws_state == "已连接" and time.time() - client.last_event_ts < 10:
        return
    now = datetime.now()
    for t in rows:
        if t["status"] not in ("queued", "running") or not t["prompt_id"]:
            continue
        try:
            started = datetime.strptime(t["created_at"], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if (now - started).total_seconds() > 10:
            try:
                client.finalize_from_history(t["prompt_id"])
            except Exception:
                pass


def queue_positions():
    """{prompt_id: 排队序号(1 起)},2 秒缓存避免每次轮询都打 ComfyUI。"""
    def _fetch():
        try:
            return client.queue()
        except ComfyError:
            return {}
    q = cached("queue", 2, _fetch)
    out = {}
    for i, it in enumerate(q.get("queue_pending") or []):
        if isinstance(it, list) and len(it) > 1:
            out[str(it[1])] = i + 1
    return out


@app.get("/api/tasks")
def api_tasks():
    ids = [int(x) for x in (request.args.get("ids") or "").split(",") if x.strip().isdigit()]
    if not ids:
        return err("缺少 ids")
    marks = ",".join("?" * len(ids))
    rows = db.query(f"SELECT * FROM tasks WHERE id IN ({marks})", ids)
    reconcile_active_tasks(rows)
    rows = db.query(f"SELECT * FROM tasks WHERE id IN ({marks}) ORDER BY id DESC", ids)
    img_rows = db.query(f"SELECT * FROM images WHERE task_id IN ({marks}) ORDER BY id", ids)
    by_task = {}
    for im in img_rows:
        by_task.setdefault(im["task_id"], []).append(im)
    qpos = {}
    if any(t["status"] == "queued" for t in rows):
        qpos = queue_positions()  # 仅在有排队任务时查询,避免空轮询打 ComfyUI
    tasks = []
    for t in rows:
        item = serialize_task(t, by_task.get(t["id"], []))
        if t["status"] == "queued" and t["prompt_id"]:
            item["queue_pos"] = qpos.get(t["prompt_id"])
        tasks.append(item)
    return {"tasks": tasks}


@app.get("/api/tasks/recent")
def api_tasks_recent():
    limit = min(int(request.args.get("limit", 8) or 8), 50)
    rows = db.query("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,))
    reconcile_active_tasks(rows)
    rows = db.query("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,))
    by_task = {}
    if rows:
        marks = ",".join("?" * len(rows))
        img_rows = db.query(
            f"SELECT * FROM images WHERE task_id IN ({marks}) ORDER BY id",
            [r["id"] for r in rows])
        for im in img_rows:
            by_task.setdefault(im["task_id"], []).append(im)
    return {"tasks": [serialize_task(t, by_task.get(t["id"], [])) for t in rows]}


@app.get("/api/tasks/<int:tid>")
def api_task_get(tid):
    t = db.query_one("SELECT * FROM tasks WHERE id=?", (tid,))
    if not t:
        return err("任务不存在", 404)
    reconcile_active_tasks([t])
    t = db.query_one("SELECT * FROM tasks WHERE id=?", (tid,))
    images = db.query("SELECT * FROM images WHERE task_id=? ORDER BY id", (tid,))
    return {"task": serialize_task(t, images)}


@app.post("/api/tasks/<int:tid>/cancel")
def api_task_cancel(tid):
    t = db.query_one("SELECT * FROM tasks WHERE id=?", (tid,))
    if not t:
        return err("任务不存在", 404)
    if t["status"] not in ("queued", "running"):
        return {"ok": True, "status": t["status"]}
    try:
        if t["status"] == "queued":
            client.queue_delete([t["prompt_id"]])
        else:
            client.interrupt(t["prompt_id"])
    except ComfyError as e:
        return err(e, 502)
    db.execute("UPDATE tasks SET status='canceled', finished_at=datetime('now','localtime') "
               "WHERE id=?", (tid,))
    return {"ok": True}


# ---------- 图片代理(带磁盘缓存) ----------

IMG_CACHE = db.DATA_DIR / "cache" / "img"
IMG_CACHE_MAX_BYTES = 800 * 1024 * 1024  # 缓存目录上限,超过时淘汰最旧的 1/4


def _img_cache_paths(key):
    import hashlib
    h = hashlib.sha1(key.encode()).hexdigest()
    return IMG_CACHE / h, IMG_CACHE / (h + ".json")


def img_cache_get(key):
    """命中返回 (文件路径, content_type)。缩略图/封面内容不可变,不做 TTL。"""
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
            _img_cache_prune()
    except OSError:
        pass


def _img_cache_prune():
    files = [(f.stat().st_mtime, f) for f in IMG_CACHE.glob("*") if not f.name.endswith(".json")]
    total = sum(f.stat().st_size for _, f in files)
    if total <= IMG_CACHE_MAX_BYTES:
        return
    files.sort()
    for _, f in files[: max(1, len(files) // 4)]:
        f.unlink(missing_ok=True)
        (IMG_CACHE / (f.name + ".json")).unlink(missing_ok=True)


@app.get("/image")
def image_proxy():
    filename = request.args.get("filename", "")
    if not filename:
        return err("缺少 filename")
    subfolder = request.args.get("subfolder", "")
    img_type = request.args.get("type", "output")
    preview = request.args.get("preview")
    # 仅缩略图落盘缓存(原图体积大,仍实时回源)
    ck = None
    if preview:
        ck = f"img:{img_type}:{subfolder}:{filename}:{preview}"
        cached_path, cached_ct = img_cache_get(ck)
        if cached_path:
            return Response(cached_path.read_bytes(),
                            content_type=cached_ct or "image/jpeg",
                            headers={"Cache-Control": "public, max-age=604800"})
    try:
        r = client.download_image(filename, subfolder, img_type, preview)
    except ComfyError as e:
        return err(e, 502)
    body = r.content
    r.close()
    if ck:
        img_cache_store(ck, body, r.headers.get("Content-Type", "image/jpeg"))
    headers = {"Cache-Control": "public, max-age=604800"}
    if request.args.get("dl"):
        # 中文前缀的文件名不能直接进 HTTP 头,按 RFC 5987 提供 UTF-8 文件名
        ext = os.path.splitext(filename)[1] or ".png"
        ascii_name = f"comfyweb_{uuid4().hex[:8]}{ext}"
        utf8_name = urlquote(filename)
        headers["Content-Disposition"] = (
            f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{utf8_name}')
    return Response(body,
                    content_type=r.headers.get("Content-Type", "image/png"),
                    headers=headers)


# ---------- 队列(设置页) ----------

@app.get("/api/queue")
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


@app.post("/api/queue/clear")
def api_queue_clear():
    try:
        client.queue_clear()
    except ComfyError as e:
        return err(e, 502)
    return {"ok": True}


@app.post("/api/cache/clear")
def api_cache_clear():
    """清空模型列表缓存(在 ComfyUI 加了新模型后手动刷新)。"""
    with _cache_lock:
        for k in [k for k in _cache if k.startswith("models:")]:
            _cache.pop(k, None)
    return {"ok": True}


@app.post("/api/queue/interrupt")
def api_queue_interrupt():
    try:
        client.interrupt()
    except ComfyError as e:
        return err(e, 502)
    return {"ok": True}


@app.post("/api/gallery/image/<int:img_id>/delete")
def api_gallery_delete(img_id):
    row = db.query_one("SELECT id FROM images WHERE id=?", (img_id,))
    if not row:
        return err("图片不存在", 404)
    db.execute("DELETE FROM images WHERE id=?", (img_id,))
    return {"ok": True}


@app.get("/healthz")
def healthz():
    return "ok"


db.init_db()
migrate_tpl_files()
client.ensure_ws()


# ---------- ComfyUI 数据预热(WS 连上后自动执行) ----------

_last_warm = [0.0]
_warm_lock = threading.Lock()


def warm_caches():
    """连上 ComfyUI 后后台拉取模型列表/服务器工作流,页面打开即有数据。"""
    now = time.time()
    with _warm_lock:
        if now - _last_warm[0] < 60:
            return
        _last_warm[0] = now
    if not db.get_setting("comfy_url"):
        return
    for folder in LOCAL_MODEL_DIRS:
        try:
            cached("models:" + folder, 120, lambda f=folder: client.models(f))
        except ComfyError:
            continue
    try:
        remote_workflow_names()
    except ComfyError:
        pass


def remote_workflow_names():
    return cached("udwf:list", 60, lambda: client.userdata_list("workflows"))


client.on_connect = warm_caches

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5012")), threaded=True)
