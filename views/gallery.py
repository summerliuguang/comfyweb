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


@bp.get("/gallery")
def page_gallery():
    where, args, cur = _gallery_filter()
    page = _parse_page()
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
    opts = library.filter_options()
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


@bp.get("/gallery/image/<int:img_id>")
def page_gallery_detail(img_id):
    """任务图详情页(仅 images 表记录;筛选在 URL 中保留以支撑前后翻页)。"""
    import json as _json
    from views.helpers import image_url

    q = request.args.get("q", "").strip()
    conds, args = [], []
    if q:
        conds.append("(t.prompt_text LIKE ? OR t.params_json LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    for key, col in (("wf", "t.workflow_name"), ("model", "t.model"), ("lora", "t.lora"),
                     ("cat", "t.category"), ("batch", "t.batch")):
        val = request.args.get(key, "").strip()
        if val:
            conds.append(f"{col} LIKE ?")
            args.append(f"%{val}%")
    rng = request.args.get("range", "all")
    if rng in ("today", "7d", "30d"):
        span = {"today": "-1 day", "7d": "-7 days", "30d": "-30 days"}[rng]
        conds.append("t.created_at >= datetime('now','localtime',?)")
        args.append(span)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""

    row = db.query_one(
        "SELECT i.*, t.workflow_id, t.workflow_name, t.prompt_text, t.seed, t.params_json, "
        "t.model, t.lora, t.status, t.category, t.batch, "
        "t.created_at AS task_created, t.count "
        "FROM images i JOIN tasks t ON t.id=i.task_id WHERE i.id=?", (img_id,))
    if not row:
        return "图片不存在(可能已被删除,或 ComfyUI 输出文件已清理)", 404
    prev_row = next_row = None
    if conds:
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
    item["params"] = _json.loads(row["params_json"] or "[]")
    item["model_short"] = re.sub(r"^.*[\\/]", "", row["model"] or "") if row["model"] else ""
    item["url"] = image_url(row["filename"], row["subfolder"], row["type"])
    item["download"] = image_url(row["filename"], row["subfolder"], row["type"], dl=True)
    item["prev_id"] = prev_row["id"] if prev_row else None
    item["next_id"] = next_row["id"] if next_row else None
    base = "FROM images i JOIN tasks t ON t.id=i.task_id " + where
    pos_where = " AND i.id < ?" if conds else " WHERE i.id < ?"
    item["pos"] = db.query_one(
        f"SELECT COUNT(*) AS n {base}{pos_where}", args + [img_id])["n"] + 1
    item["total"] = db.query_one(f"SELECT COUNT(*) AS n {base}", args)["n"]
    item["total"] = max(item["total"], 1)

    # 邻图数据(任务记录集合内,供 AJAX 切换)
    rows = db.query(
        f"SELECT i.id, i.filename, i.subfolder, i.type, i.fav, t.workflow_id, t.workflow_name, "
        f"t.model, t.prompt_text, t.created_at AS task_created "
        f"FROM images i JOIN tasks t ON t.id=i.task_id {where} "
        f"ORDER BY i.id DESC LIMIT 400", args)
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
    qs = urlencode({k: v for k, v in request.args.items() if v.strip()})
    return render_template("gallery_detail.html", item=item, qs=qs,
                           neighbors=neighbors, idx=cur_idx, active="gallery")


@bp.post("/api/gallery/image/<int:img_id>/delete")
def api_gallery_delete(img_id):
    row = db.query_one("SELECT id FROM images WHERE id=?", (img_id,))
    if not row:
        return jsonify({"error": "图片不存在"}), 404
    db.execute("DELETE FROM images WHERE id=?", (img_id,))
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
