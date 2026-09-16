"""画廊与收藏:瀑布流、筛选、详情(邻图数据)、收藏/删除。"""
import json
import re
from urllib.parse import urlencode

from flask import Blueprint, render_template, request

import db

from views.helpers import err, image_url

bp = Blueprint("gallery", __name__)

PAGE_SIZE = 24
GALLERY_RANGES = {"today": "今天", "7d": "近 7 天", "30d": "近 30 天", "all": "全部"}


def _gallery_filter():
    """解析画廊筛选参数,返回 (where_sql, args, current)。"""
    conds, args, cur = [], [], {}
    q = request.args.get("q", "").strip()
    if q:
        conds.append("(t.prompt_text LIKE ? OR t.params_json LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    for key, col in (("wf", "t.workflow_name"), ("model", "t.model"), ("lora", "t.lora"),
                     ("cat", "t.category"), ("batch", "t.batch")):
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


def _parse_page():
    try:
        return max(1, int(request.args.get("page", 1)))
    except ValueError:
        return 1


@bp.get("/gallery")
def page_gallery():
    where, args, cur = _gallery_filter()
    q = cur["q"]
    page = _parse_page()
    total = db.query_one(
        f"SELECT COUNT(*) AS n FROM images i JOIN tasks t ON t.id=i.task_id {where}",
        args)["n"]
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, pages)
    rows = db.query(
        f"SELECT i.id, i.filename, i.subfolder, i.type, i.fav, t.workflow_name, t.model, "
        f"t.prompt_text, t.category, t.batch, t.created_at AS task_created, i.created_at AS img_created "
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
        "cats": [r["category"] for r in db.query(
            "SELECT DISTINCT category FROM tasks WHERE category!='' ORDER BY 1")],
        "batches": [r["batch"] for r in db.query(
            "SELECT DISTINCT batch FROM tasks WHERE batch!='' ORDER BY 1 DESC")],
    }
    filt = {k: v.strip() for k, v in request.args.items() if k != "page" and v.strip()}
    qs = urlencode(filt)
    return render_template("gallery.html", items=items, q=q, page=page, pages=pages,
                           total=total, opts=opts, cur=cur, ranges=GALLERY_RANGES,
                           qs=qs, active="gallery")


def _detail_qs():
    """详情页翻页要带上的筛选参数(不含 page)。"""
    return urlencode({k: v for k, v in request.args.items() if v.strip()})


@bp.get("/api/gallery/image/<int:img_id>/params")
def api_gallery_image_params(img_id):
    """单图的完整参数(sheet 展开时按需拉取,切换图片后刷新)。"""
    row = db.query_one(
        "SELECT t.workflow_name, t.model, t.lora, t.prompt_text, t.seed, "
        "t.category, t.batch, t.params_json, t.created_at AS task_created "
        "FROM images i JOIN tasks t ON t.id=i.task_id WHERE i.id=?", (img_id,))
    if not row:
        return err("图片不存在", 404)
    return {"workflow_name": row["workflow_name"],
            "model": row["model"],
            "lora": row["lora"],
            "prompt_text": row["prompt_text"],
            "seed": row["seed"],
            "category": row["category"],
            "batch": row["batch"],
            "created_at": row["task_created"],
            "params": json.loads(row["params_json"] or "[]")}


@bp.get("/gallery/image/<int:img_id>")
def page_gallery_detail(img_id):
    where, args, _cur = _gallery_filter()
    row = db.query_one(
        "SELECT i.*, t.workflow_id, t.workflow_name, t.prompt_text, t.seed, t.params_json, "
        "t.model, t.lora, t.status, t.category, t.batch, "
        "t.created_at AS task_created, t.count "
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
        f"SELECT i.id, i.filename, i.subfolder, i.type, i.fav, t.workflow_id, t.workflow_name, "
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
            "f": bool(r["fav"]),
        })
    return render_template("gallery_detail.html", item=item, qs=_detail_qs(),
                           neighbors=neighbors, idx=cur_idx, active="gallery")


@bp.post("/api/gallery/image/<int:img_id>/delete")
def api_gallery_delete(img_id):
    row = db.query_one("SELECT id FROM images WHERE id=?", (img_id,))
    if not row:
        return err("图片不存在", 404)
    db.execute("DELETE FROM images WHERE id=?", (img_id,))
    return {"ok": True}


@bp.post("/api/gallery/image/<int:img_id>/fav")
def api_gallery_fav(img_id):
    """收藏/取消收藏(切换状态)。"""
    row = db.query_one("SELECT fav FROM images WHERE id=?", (img_id,))
    if not row:
        return err("图片不存在", 404)
    fav = 0 if row["fav"] else 1
    db.execute("UPDATE images SET fav=? WHERE id=?", (fav, img_id))
    return {"ok": True, "fav": bool(fav)}


@bp.get("/favorites")
def page_favorites():
    """收藏页:与画廊同款瀑布流,只看收藏图片。"""
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
