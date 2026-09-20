"""画廊与收藏:全库瀑布流(整理库索引为数据源)、筛选、任务详情、收藏/删除。

画廊数据源是 library 索引(files 表)——覆盖归档图、手工子目录图与新生成图;
有任务记录的图保留原详情页(参数/收藏/再次生成),其余经灯箱看原图。
"""
import json
import re
from urllib.parse import urlencode

from flask import Blueprint, jsonify, render_template, request

import db
import library
from views.helpers import cached

bp = Blueprint("gallery", __name__)

PAGE_SIZE = 24
GALLERY_RANGES = {"today": "今天", "7d": "近 7 天", "30d": "近 30 天", "all": "全部"}


def _gallery_filter():
    """解析画廊筛选参数 → (where_sql, args, current)。列名来自 files 表白名单。"""
    conds, args, cur = [], [], {}
    q = request.args.get("q", "").strip()
    if q:
        like = f"%{q}%"
        conds.append("(prompt LIKE ? OR tags LIKE ? OR batch LIKE ? "
                     "OR workflow LIKE ? OR filename LIKE ?)")
        args += [like] * 5
        cur["q"] = q
    for key, col in (("wf", "workflow"), ("model", "model"), ("cat", "category"),
                     ("batch", "batch"), ("lora", "lora")):
        val = request.args.get(key, "").strip()
        if val:
            conds.append(f"{col} LIKE ?")
            args.append(f"%{val}%")
            cur[key] = val
    rng = request.args.get("range", "all")
    if rng in ("today", "7d", "30d"):
        span = {"today": "-1 day", "7d": "-7 days", "30d": "-30 days"}[rng]
        conds.append("created_at >= datetime('now','localtime',?)")
        args.append(span)
        cur["range"] = rng
    where = (" AND ".join(conds)) if conds else ""
    return where, args, cur


def _parse_page():
    try:
        return max(1, int(request.args.get("page", 1)))
    except ValueError:
        return 1


def _short(s, limit=34):
    s = re.sub(r"^.*[\\/]", "", s or "")
    return s if len(s) <= limit else s[:limit - 1] + "…"


def _gallery_items(where, args, page):
    """按页构建画廊条目(HTML 与 JSON 分页接口共用)。"""
    rows, total = library.page_query(where, args, page, PAGE_SIZE)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, pages)
    if page != _parse_page():  # 超出末页时回落
        rows, total = library.page_query(where, args, page, PAGE_SIZE)
    img_ids = library.task_img_id_map([r["filename"] for r in rows])
    items = []
    for r in rows:
        label = (r["prompt"] or r["batch"] or r["workflow"]
                 or ("未分类 · " + _short(r["filename"]))).strip()
        t = img_ids.get(r["filename"]) or {}
        items.append({
            "rowid": r["rowid"],
            "task_img_id": t.get("img_id"),
            "fav": bool(t.get("fav")),
            "thumb": f"/libthumb/{r['rowid']}",
            "url": f"/libmedia/{r['rowid']}",
            "label": _short(label, 60),
        })
    return items, page, pages, total


@bp.get("/api/gallery/items")
def api_gallery_items():
    """画廊分页 JSON(无限滚动追加加载用)。"""
    where, args, _cur = _gallery_filter()
    items, page, pages, total = _gallery_items(where, args, _parse_page())
    return {"items": items, "page": page, "pages": pages, "total": total}


@bp.get("/gallery")
def page_gallery():
    where, args, cur = _gallery_filter()
    items, page, pages, total = _gallery_items(where, args, _parse_page())
    # 5 个 SELECT DISTINCT 全列扫描,TTL 缓存兜住首页开销;新图最迟 60s 进下拉
    opts = cached("gallery_filters", 60, library.filter_options)
    opts = {
        "workflows": opts["workflow"],
        "models": [(m, _short(m)) for m in opts["model"]],
        "loras": [(l, _short(l)) for l in opts["lora"]],
        "cats": opts["category"],
        "batches": opts["batch"],
    }
    filt = {k: v.strip() for k, v in request.args.items() if k != "page" and v.strip()}
    qs = urlencode(filt)
    return render_template("gallery.html", items=items, q=cur.get("q", ""), page=page, pages=pages,
                           total=total, opts=opts, cur=cur, ranges=GALLERY_RANGES,
                           qs=qs, active="gallery")


@bp.get("/api/gallery/image/<int:img_id>/params")
def api_gallery_image_params(img_id):
    """单图的完整参数(sheet 展开时按需拉取,切换图片后刷新)。"""
    row = db.query_one(
        "SELECT t.workflow_name, t.model, t.lora, t.prompt_text, t.seed, "
        "t.category, t.batch, t.params_json, t.created_at AS task_created "
        "FROM images i JOIN tasks t ON t.id=i.task_id WHERE i.id=?", (img_id,))
    if not row:
        return jsonify({"error": "图片不存在"}), 404
    return {"workflow_name": row["workflow_name"],
            "model": row["model"],
            "lora": row["lora"],
            "prompt_text": row["prompt_text"],
            "seed": row["seed"],
            "category": row["category"],
            "batch": row["batch"],
            "created_at": row["task_created"],
            "params": json.loads(row["params_json"] or "[]")}


@bp.get("/gallery/view/<int:rowid>")
def page_gallery_view(rowid):
    """详情页(全库索引定位):位置/翻页/邻图与画廊网格同一数据源。

    上滑详情面板:有任务记录的图显示完整生成参数(收藏/删除/再次生成),
    其余显示库内信息(模型/标签/大小)。
    """
    where, args, _cur = _gallery_filter()
    ctx = library.view_neighbors(rowid, where, args)
    if not ctx:
        return "图片不存在(可能已被删除)", 404

    from views.helpers import image_url

    # 任务记录增强(按文件名关联):详情面板参数、收藏角标、再次生成
    tmap = {}
    names = [r["filename"] for r in ctx["neighbors"]] + [ctx["row"]["filename"]]
    img_ids = library.task_img_id_map(names)
    real_ids = [v["img_id"] for v in img_ids.values() if v["img_id"]]
    if real_ids:
        marks = ",".join("?" * len(real_ids))
        for t in db.query(
                f"SELECT i.id, i.filename, i.subfolder, i.type, i.fav, t.workflow_id, "
                f"t.workflow_name, t.prompt_text, t.model, t.seed, t.created_at, t.params_json "
                f"FROM images i JOIN tasks t ON t.id=i.task_id WHERE i.id IN ({marks})",
                real_ids):
            tmap[t["filename"]] = dict(t)

    neighbors = []
    for r in ctx["neighbors"]:
        t = tmap.get(r["filename"])
        rid = r["rowid"]
        if t:
            url = image_url(r["filename"], t["subfolder"], t["type"])
            dl = image_url(r["filename"], t["subfolder"], t["type"], dl=True)
        else:
            url, dl = f"/libmedia/{rid}", f"/libmedia/{rid}?dl=1"
        neighbors.append({
            "rid": rid, "id": (t or {}).get("id"), "task": bool(t),
            "w": (t or {}).get("workflow_id"),
            "url": url, "dl": dl, "thumb": f"/libthumb/{rid}",
            "prompt": (t or {}).get("prompt_text") or f"未分类 · {r['filename']}",
            "wf": (t or {}).get("workflow_name") or "",
            "m": _short(r["model"]),
            "ts": (t or {}).get("created_at") or r["created_at"],
            "f": bool((t or {}).get("fav")),
        })

    cur_row = ctx["row"]
    t = tmap.get(cur_row["filename"])
    if t:
        item = {
            "task": True, "img_id": t["id"], "fav": bool(t["fav"]),
            "prompt_text": t["prompt_text"],
            "workflow_name": t["workflow_name"],
            "model_short": _short(t["model"]),
            "task_created": t["created_at"],
            "seed": t["seed"],
            "params": json.loads(t["params_json"] or "[]"),
            "url": neighbors[[n["rid"] for n in neighbors].index(rowid)]["url"],
            "download": neighbors[[n["rid"] for n in neighbors].index(rowid)]["dl"],
        }
    else:
        item = {
            "task": False, "img_id": None, "fav": False,
            "prompt_text": f"未分类 · {cur_row['filename']}",
            "workflow_name": cur_row["workflow"] or cur_row["batch"] or "",
            "model_short": _short(cur_row["model"]),
            "task_created": cur_row["created_at"],
            "seed": cur_row["seed"],
            "params": [],
            "url": f"/libmedia/{rowid}",
            "download": f"/libmedia/{rowid}?dl=1",
        }
    idx = next((i for i, n in enumerate(neighbors) if n["rid"] == rowid), 0)
    qs = urlencode({k: v for k, v in request.args.items() if v.strip()})
    return render_template("gallery_detail.html", item=item, qs=qs,
                           neighbors=neighbors, idx=idx,
                           pos=ctx["pos"], total=ctx["total"],
                           newer_rid=ctx["newer_rid"], older_rid=ctx["older_rid"],
                           active="gallery")


@bp.get("/api/library/image/<int:rowid>/info")
def api_library_image_info(rowid):
    """非任务图的详情面板数据(库内信息 + 从 PNG 内嵌块解析出的生成参数)。"""
    r = library.get(rowid)
    if not r:
        return jsonify({"error": "图片不存在"}), 404
    params = None
    if r["params_json"]:
        try:
            params = json.loads(r["params_json"])
        except ValueError:
            params = None
    return {"filename": r["filename"], "model": r["model"], "category": r["category"],
            "batch": r["batch"], "workflow": r["workflow"], "size": r["size"],
            "created_at": r["created_at"], "prompt": r["prompt"] or None,
            "seed": r["seed"],
            "tags": json.loads(r["tags"] or "[]"),
            "params": params}


@bp.get("/gallery/image/<int:img_id>")
def page_gallery_detail(img_id):
    """旧详情链接兼容:定位同名整理库行后跳转到全库详情。"""
    row = db.query_one("SELECT filename FROM images WHERE id=?", (img_id,))
    if not row:
        return "图片不存在(可能已被删除,或 ComfyUI 输出文件已清理)", 404
    lib = library.indexed(row["filename"])
    if not lib:
        return "图片未入库(整理库索引缺失)", 404
    qs = urlencode({k: v for k, v in request.args.items() if v.strip()})
    from flask import redirect
    return redirect(f"/gallery/view/{lib['rowid']}" + (f"?{qs}" if qs else ""), 302)


@bp.post("/api/library/image/<int:rowid>/delete")
def api_library_image_delete(rowid):
    """彻底删除图片:正本/缩略图/本地缓冲/索引/任务图片记录一并删除,记墓碑
    防 GPU 同步回流复活;tasks 行保留(同任务多图与再次生成不受影响)。"""
    ok, error = library.delete_image(rowid)
    if not ok:
        return jsonify({"error": error}), (404 if error == "图片不存在" else 502)
    return {"ok": True}


@bp.post("/api/gallery/image/<int:img_id>/fav")
def api_gallery_fav(img_id):
    """收藏/取消收藏(切换状态)。"""
    row = db.query_one("SELECT fav FROM images WHERE id=?", (img_id,))
    if not row:
        return jsonify({"error": "图片不存在"}), 404
    fav = 0 if row["fav"] else 1
    db.execute("UPDATE images SET fav=? WHERE id=?", (fav, img_id))
    return {"ok": True, "fav": bool(fav)}


@bp.get("/favorites")
def page_favorites():
    """收藏页:与画廊同款瀑布流,只看收藏图片(任务图)。"""
    from views.helpers import image_url

    page = _parse_page()
    total = db.query_one(
        "SELECT COUNT(*) AS n FROM images i JOIN tasks t ON t.id=i.task_id WHERE i.fav=1")["n"]
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, pages)
    rows = db.query(
        "SELECT i.id, i.filename, i.subfolder, i.type, i.fav, t.workflow_name, t.model, "
        "t.prompt_text, t.category, t.batch, t.created_at AS task_created, i.created_at AS img_created "
        "FROM images i JOIN tasks t ON t.id=i.task_id WHERE i.fav=1 "
        "ORDER BY i.id DESC LIMIT ? OFFSET ?",
        (PAGE_SIZE, (page - 1) * PAGE_SIZE))
    items = []
    for r in rows:
        it = dict(r)
        it["thumb"] = image_url(r["filename"], r["subfolder"], r["type"], preview="webp;jpeg;70")
        items.append(it)
    qs = ""
    return render_template("favorites.html", items=items, page=page, pages=pages,
                           total=total, qs=qs, active="favorites")


def register(app):
    app.register_blueprint(bp)
