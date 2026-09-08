"""ComfyWeb — ComfyUI 简易生成站(Flask 入口)。"""
import json
import os
import re
import threading
import time
from datetime import datetime
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

from flask import (Flask, Response, jsonify, render_template, request,
                   stream_with_context)

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
    val = fn()
    with _cache_lock:
        _cache[key] = (now, val)
    return val


def err(msg, code=400):
    return jsonify({"error": str(msg)}), code


@app.before_request
def same_origin_only():
    """控制接口只接受同源 POST(nginx 层另有 basic auth)。"""
    if request.method == "POST":
        origin = request.headers.get("Origin") or request.headers.get("Referer")
        if origin:
            netloc = urlsplit(origin).netloc
            if netloc and netloc != request.host:
                return err("拒绝跨源请求", 403)


# ---------- 工作流模板文件 ----------

TPL_FILE_RE = re.compile(r"^wf_[0-9]+\.json$")


def tpl_path(filename):
    """模板文件名只能是我们自己生成的形式,目录锁定在 data/workflows 内。"""
    if not TPL_FILE_RE.match(str(filename)):
        raise ValueError(f"非法模板文件名: {filename!r}")
    p = (db.WF_DIR / filename).resolve()
    if p.parent != db.WF_DIR.resolve():
        raise ValueError("模板路径越界")
    return p


def load_tpl(row):
    with open(tpl_path(row["filename"]), encoding="utf-8") as f:
        return json.load(f)


def save_tpl_file(wid, data):
    """模板文件名一律由行 id(整数)推导,写入路径固定在 data/workflows 内。"""
    target = db.WF_DIR / ("wf_%d.json" % int(wid))
    if target.resolve().parent != db.WF_DIR.resolve():
        raise ValueError("模板路径越界")
    target.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


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


@app.get("/gallery")
def page_gallery():
    q = request.args.get("q", "").strip()
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    where, args = "", []
    if q:
        where = "WHERE t.prompt_text LIKE ? OR t.params_json LIKE ?"
        args = [f"%{q}%", f"%{q}%"]
    total = db.query_one(f"SELECT COUNT(*) AS n FROM images i JOIN tasks t ON t.id=i.task_id {where}", args)["n"]
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, pages)
    rows = db.query(
        f"SELECT i.id, i.filename, i.subfolder, i.type, t.workflow_name, t.prompt_text, "
        f"t.created_at AS task_created, i.created_at AS img_created "
        f"FROM images i JOIN tasks t ON t.id=i.task_id {where} "
        f"ORDER BY i.id DESC LIMIT ? OFFSET ?",
        args + [PAGE_SIZE, (page - 1) * PAGE_SIZE])
    items = []
    for r in rows:
        it = dict(r)
        it["thumb"] = image_url(r["filename"], r["subfolder"], r["type"], preview="webp;jpeg;70")
        items.append(it)
    return render_template("gallery.html", items=items, q=q, page=page, pages=pages,
                           total=total, active="gallery")


@app.get("/gallery/image/<int:img_id>")
def page_gallery_detail(img_id):
    row = db.query_one(
        "SELECT i.*, t.workflow_id, t.workflow_name, t.prompt_text, t.seed, t.params_json, "
        "t.status, t.created_at AS task_created, t.count "
        "FROM images i JOIN tasks t ON t.id=i.task_id WHERE i.id=?", (img_id,))
    if not row:
        return "图片不存在(可能已被删除,或 ComfyUI 输出文件已清理)", 404
    item = dict(row)
    item["params"] = json.loads(row["params_json"] or "[]")
    item["url"] = image_url(row["filename"], row["subfolder"], row["type"])
    item["download"] = image_url(row["filename"], row["subfolder"], row["type"], dl=True)
    return render_template("gallery_detail.html", item=item, active="gallery")


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
    """本机(ComfyUI 侧)已安装的模型文件列表。"""
    folder = request.args.get("type", "checkpoints")
    if folder not in LOCAL_MODEL_DIRS:
        return err("无效的类型")
    try:
        files = cached("models:" + folder, 120, lambda f=folder: client.models(f))
    except ComfyError as e:
        return err(e, 502)
    return {"files": sorted(str(f) for f in files)}


@app.get("/civimg")
def civitai_image_proxy():
    """civitai CDN 图片代理(局域网设备一般无法直连 civitai)。"""
    u = request.args.get("u", "")
    try:
        r = civitai.fetch_image(u)
    except civitai.CivitaiError as e:
        return err(e, 502)
    return Response(stream_with_context(r.iter_content(16384)),
                    content_type=r.headers.get("Content-Type", "image/jpeg"),
                    headers={"Cache-Control": "public, max-age=604800"})


@app.get("/settings")
def page_settings():
    return render_template("settings.html", comfy_url=db.get_setting("comfy_url"),
                           civitai_proxy=db.get_setting("civitai_proxy"),
                           civitai_token=db.get_setting("civitai_token"),
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
        db.set_setting("civitai_token", (data.get("civitai_token") or "").strip())
    if "civitai_nsfw" in data:
        db.set_setting("civitai_nsfw", "1" if data.get("civitai_nsfw") else "0")
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
    """列出 ComfyUI 用户目录里已保存的工作流文件。"""
    try:
        names = client.userdata_list("workflows")
    except ComfyError as e:
        return err(e, 502)
    return {"workflows": sorted(str(n) for n in names if str(n).endswith(".json"))}


@app.post("/api/workflows/import_remote")
def api_workflow_import_remote():
    """拉取 ComfyUI 里已保存的工作流,UI 格式转 API 格式后走导入评审。"""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name or "/" in name or "\\" in name or ".." in name or not name.endswith(".json"):
        return err("无效的工作流文件名")
    try:
        uiwf = client.userdata_read("workflows/" + name)
    except ComfyError as e:
        return err(e, 502)
    classes = {n.get("type") for n in (uiwf.get("nodes") or []) if n.get("type")}
    object_info = fetch_object_info(classes)
    missing = {c for c in classes if not object_info.get(c)}
    try:
        wf = wfmod.ui_to_api(uiwf, object_info)
        tpl = wfmod.parse_workflow(wf, object_info)
    except wfmod.WorkflowParseError as e:
        return err(e)
    for p in tpl["params"]:
        if p.get("widget") == "select" and p.get("dynamic"):
            p["options"] = dynamic_options_for(p)
    warnings = []
    if missing:
        warnings.append("以下节点类未取到定义(可能缺插件): " + ", ".join(sorted(missing)))
    return {"workflow": wf, "params": tpl["params"], "batch_node": tpl["batch_node"],
            "node_count": tpl["node_count"], "warnings": warnings,
            "name": name[:-len(".json")]}


@app.post("/api/workflows")
def api_workflow_create():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip() or \
        datetime.now().strftime("工作流 %Y-%m-%d %H:%M")
    wf = data.get("workflow")
    params = sanitize_params(data.get("params"))
    if not isinstance(wf, dict) or not wf:
        return err("缺少工作流 JSON")
    if not params:
        return err("没有可用的参数定义")
    cur = db.execute("INSERT INTO workflows(name, filename) VALUES(?, '')", (name,))
    wid = cur.lastrowid
    filename = f"wf_{wid}.json"
    save_tpl_file(wid, {"version": 1, "name": name, "workflow": wf,
                        "params": params, "batch_node": data.get("batch_node")})
    db.execute("UPDATE workflows SET filename=? WHERE id=?", (filename, wid))
    return {"ok": True, "id": wid}


@app.get("/api/workflows/<int:wid>")
def api_workflow_get(wid):
    row = db.query_one("SELECT * FROM workflows WHERE id=?", (wid,))
    if not row:
        return err("模板不存在", 404)
    tpl = complete_select_options(load_tpl(row))
    wfmod.sort_params(tpl["params"])
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
    save_tpl_file(wid, tpl)
    db.execute("UPDATE workflows SET filename=? WHERE id=?", (f"wf_{wid}.json", wid))
    db.execute("UPDATE workflows SET name=? WHERE id=?", (tpl["name"], wid))
    if "enabled" in data:
        db.execute("UPDATE workflows SET enabled=? WHERE id=?",
                   (1 if data.get("enabled") else 0, wid))
    return {"ok": True}


@app.post("/api/workflows/<int:wid>/delete")
def api_workflow_delete(wid):
    row = db.query_one("SELECT * FROM workflows WHERE id=?", (wid,))
    if not row:
        return err("模板不存在", 404)
    tpl_path(row["filename"]).unlink(missing_ok=True)
    db.execute("DELETE FROM workflows WHERE id=?", (wid,))
    return {"ok": True}


# ---------- 生成 ----------

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
                "params_json, status, error, count, finished_at) "
                "VALUES(?,?,?,?,?,'error',?,1,datetime('now','localtime'))",
                (row["id"], row["name"], prompt_text, seed,
                 json.dumps(params_display(tpl, values, seed), ensure_ascii=False), str(e)))
            task_ids.append(cur.lastrowid)
            errors.append(str(e))
            break
        cur = db.execute(
            "INSERT INTO tasks(prompt_id, workflow_id, workflow_name, prompt_text, seed, "
            "params_json, status, count) VALUES(?,?,?,?,?,?,'queued',?)",
            (prompt_id, row["id"], row["name"], prompt_text, seed,
             json.dumps(params_display(tpl, values, seed), ensure_ascii=False),
             count if use_batch else 1))
        task_ids.append(cur.lastrowid)
    client.ensure_ws()
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
    """WS 掉线兜底:排队/执行超过 10 秒的任务直接查 history 对账。"""
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
    return {"tasks": [serialize_task(t, by_task.get(t["id"], [])) for t in rows]}


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


# ---------- 图片代理 ----------

@app.get("/image")
def image_proxy():
    filename = request.args.get("filename", "")
    if not filename:
        return err("缺少 filename")
    subfolder = request.args.get("subfolder", "")
    img_type = request.args.get("type", "output")
    preview = request.args.get("preview")
    try:
        r = client.download_image(filename, subfolder, img_type, preview)
    except ComfyError as e:
        return err(e, 502)
    headers = {"Cache-Control": "public, max-age=604800"}
    if request.args.get("dl"):
        headers["Content-Disposition"] = f'attachment; filename="comfyweb_{uuid4().hex[:8]}_{filename}"'
    return Response(stream_with_context(r.iter_content(16384)),
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


def migrate_tpl_filenames():
    """旧版随机文件名(wf_<hex>.json)迁移为按行 id 命名(wf_<id>.json)。"""
    for row in db.query("SELECT id, filename FROM workflows"):
        want = f"wf_{row['id']}.json"
        if row["filename"] == want:
            continue
        try:
            data = load_tpl(row)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        save_tpl_file(row["id"], data)
        db.execute("UPDATE workflows SET filename=? WHERE id=?", (want, row["id"]))
        try:
            tpl_path(row["filename"]).unlink()
        except OSError:
            pass


db.init_db()
migrate_tpl_filenames()
client.ensure_ws()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5012")), threaded=True)
