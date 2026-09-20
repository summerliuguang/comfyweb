"""生成与工作流模板:模板 CRUD/远端导入转换、生成提交、任务跟踪。

图片代理在 views/media.py(画廊/收藏共用的共享基础设施)。
"""
import json
import time
from datetime import datetime

from flask import Blueprint, render_template, request

import db
import workflow as wfmod
from comfy_client import ComfyError, client, reconcile_active_tasks

from views.helpers import PARAM_KEYS, cached, err, image_url

bp = Blueprint("gen", __name__)


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
    """动态下拉选项:/models/<folder>,当前值不在列表时置顶。
    ComfyUI 不可达时返回空并标记 dynamic_empty,前端给占位提示。"""
    folder = param["dynamic"]
    try:
        opts = [str(o) for o in cached("models:" + folder, 120,
                                       lambda f=folder: client.models(f))]
    except ComfyError:
        param["dynamic_empty"] = True
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
        node = wf.get(p["node_id"]) or {}
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

@bp.get("/")
def page_generate():
    rows = db.query("SELECT id, name FROM workflows WHERE enabled=1 ORDER BY id DESC")
    active = db.query(
        "SELECT id FROM tasks WHERE status IN ('queued','running') ORDER BY id DESC LIMIT 20")
    return render_template("generate.html", workflows=rows,
                           active_ids=[r["id"] for r in active], active="generate")


@bp.get("/workflows")
def page_workflows():
    rows = db.query("SELECT * FROM workflows ORDER BY id DESC")
    favs = [r for r in rows if r["fav"]]
    opts = {k: sorted({r[k] for r in rows if r[k]})
            for k in ("scene", "base_model", "purpose")}
    return render_template("workflows.html", workflows=rows, favs=favs, opts=opts,
                           active="workflows")


@bp.get("/workflows/import")
def page_workflow_import():
    return render_template("workflow_import.html", active="workflows")


@bp.get("/workflows/<int:wid>/edit")
def page_workflow_edit(wid):
    row = db.query_one("SELECT * FROM workflows WHERE id=?", (wid,))
    if not row:
        return "模板不存在", 404
    return render_template("workflow_edit.html", wf=row, active="workflows")


@bp.get("/api/prompts")
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


# ---------- 工作流 API ----------

@bp.post("/api/workflows/parse")
def api_workflow_parse():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return err("请粘贴工作流 JSON")
    try:
        wf = json.loads(text)
    except ValueError as e:
        return err(f"JSON 解析失败: {e}")
    if not isinstance(wf, dict):
        return err("不是有效的工作流 JSON")
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


_udwf_fail_at = [0.0]  # 最近一次远端工作流列表拉取失败时刻;30s 内不再重试长周期


_SCENE_KEYWORDS = [("图生图", "图生图"), ("重绘", "局部重绘"), ("放大", "放大"),
                   ("超分", "放大"), ("反推", "反推"), ("controlnet", "控制"),
                   ("控制", "控制"), ("文生图", "文生图"), ("润色", "润色")]
_BASE_KEYWORDS = [("flux", "Flux"), ("pony", "Pony"), ("illustrious", "Illustrious"),
                  ("noobai", "NoobAI"), ("hunyuan", "混元"), ("wan", "Wan"),
                  ("chroma", "Chroma"), ("sd3", "SD3.5"), ("xl", "SDXL"),
                  ("v1-5", "SD1.5"), ("v1.5", "SD1.5"), ("sd1", "SD1.5")]


def guess_workflow_tags(name, tpl):
    """按工作流名称与模型参数默认值猜 (场景, 底模);猜不出留空,编辑页可改。"""
    low = (name or "").lower()
    scene = next((s for kw, s in _SCENE_KEYWORDS if kw in low or kw in (name or "")), "")
    base = ""
    for p in tpl.get("params") or []:
        if p.get("dynamic") == "checkpoints" and p.get("value"):
            fname = str(p["value"]).lower()
            base = next((b for kw, b in _BASE_KEYWORDS if kw in fname), "")
            break
    return scene, base


def _apply_guessed_tags(wid, name, tpl):
    scene, base = guess_workflow_tags(name, tpl)
    if scene or base:
        db.execute("UPDATE workflows SET scene=?, base_model=? WHERE id=?",
                   (scene, base, wid))


@bp.get("/api/remote/workflows")
def api_remote_workflows():
    """列出 ComfyUI 用户目录里已保存的工作流文件(预热后直接命中缓存)。"""
    # WS 状态由常驻线程维护;明确未连接时快速失败,避免前端"读取中"挂满 3 次重试周期
    if client.ws_state not in ("已连接", "已断开,正在重连"):
        return err(f"ComfyUI {client.ws_state},请确认 ComfyUI 正在运行", 503)
    if time.time() - _udwf_fail_at[0] < 30:
        return err("刚刚连接失败,ComfyUI 可能未就绪,请稍候点「重试」", 503)
    try:
        names = remote_workflow_names()
    except ComfyError as e:
        _udwf_fail_at[0] = time.time()
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


def remote_workflow_names():
    return cached("udwf:list", 60, lambda: client.userdata_list("workflows"))


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


@bp.post("/api/workflows/import_remote")
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


@bp.post("/api/workflows/import_remote_save")
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
    _apply_guessed_tags(wid, display, tpl)
    return {"ok": True, "id": wid, "name": display, "warnings": warnings}


@bp.post("/api/workflows")
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
    _apply_guessed_tags(wid, name, {"params": params})
    return {"ok": True, "id": wid}


@bp.get("/api/workflows/<int:wid>")
def api_workflow_get(wid):
    row = db.query_one("SELECT * FROM workflows WHERE id=?", (wid,))
    if not row:
        return err("模板不存在", 404)
    tpl = complete_select_options(load_tpl(row))
    wfmod.sort_params(tpl["params"])
    if repair_template(tpl):
        db.set_workflow_template(wid, tpl)  # 修复结果落库,下次直接可用
    return {"id": row["id"], "name": row["name"], "enabled": row["enabled"],
            "fav": row["fav"], "scene": row["scene"], "base_model": row["base_model"],
            "purpose": row["purpose"],
            "params": tpl["params"], "batch_node": tpl["batch_node"],
            "workflow": tpl["workflow"]}


@bp.post("/api/workflows/<int:wid>")
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
    db.execute("UPDATE workflows SET name=? WHERE id=?", (tpl["name"], int(wid)))
    if "enabled" in data:
        db.execute("UPDATE workflows SET enabled=? WHERE id=?",
                   (1 if data.get("enabled") else 0, int(wid)))
    if "fav" in data:
        db.execute("UPDATE workflows SET fav=? WHERE id=?",
                   (1 if data.get("fav") else 0, int(wid)))
    for k in ("scene", "base_model", "purpose"):  # 列名为字面量白名单,无注入面
        if k in data:
            db.execute(f"UPDATE workflows SET {k}=? WHERE id=?",
                       ((data.get(k) or "").strip()[:50], int(wid)))
    return {"ok": True}


@bp.post("/api/workflows/<int:wid>/delete")
def api_workflow_delete(wid):
    row = db.query_one("SELECT * FROM workflows WHERE id=?", (int(wid),))
    if not row:
        return err("模板不存在", 404)
    db.delete_workflow(int(wid))
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


@bp.post("/api/generate")
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
    db.prune_tasks()
    return {"task_ids": task_ids, "error": "\n".join(errors)}


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


@bp.get("/api/tasks")
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


@bp.get("/api/tasks/recent")
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


@bp.get("/api/tasks/<int:tid>")
def api_task_get(tid):
    t = db.query_one("SELECT * FROM tasks WHERE id=?", (tid,))
    if not t:
        return err("任务不存在", 404)
    reconcile_active_tasks([t])
    t = db.query_one("SELECT * FROM tasks WHERE id=?", (tid,))
    images = db.query("SELECT * FROM images WHERE task_id=? ORDER BY id", (tid,))
    return {"task": serialize_task(t, images)}


@bp.post("/api/tasks/<int:tid>/cancel")
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


# ---------- 注册与预热 ----------

def register(app):
    app.register_blueprint(bp)
    migrate_tpl_files()


def warm():
    """WS 连上后预热远端工作流列表(ComfyError 由预热调度方兜底)。"""
    remote_workflow_names()
