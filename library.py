"""NAS 图片库(library/):分类树 + 全库索引 + 缩略图 + 收编。

目录规则(经确认的架构决策):
- 大类 = 模型名(tasks.model 去扩展名);有任务归属的图进 `模型/日期_批次或工作流/`,
  无归属的进 `未分类/<原前缀>/`(保留以后归位的线索);
- 分片"只增不改名":目录满 SHARD_LIMIT 张即封存,后续新图写入 _002 分片,
  已入索引的路径永不改变——索引因此永不失效,也不影响正在生成的图;
- 收编:扫描 output/ 根目录平铺文件(手工子目录一律不碰),能对上 comfyweb
  任务记录的按记录归位,对不上的按前缀归未分类;GPU 同步重复推送的文件名
  以索引为准跳过;
- 全库索引覆盖三类图片:library 归档、output/ 手工子目录(原地索引不移动)、
  新生成归档——是画廊"显示所有图片"的统一数据源;
- 索引正本在本机 data/library.db(SQLite 绝不在 CIFS 上开写连接,实测锁死),
  每轮变更后经 backup API 快照 + 字节拷贝 + md5 校验推送 NAS index.db;
- 缩略图经 Pillow 本地生成(最长边 512 webp q75),存 library/thumbs 平行树;
- reverse(filename) 把已收编的图还原回 output/ 并删索引行,作回滚保底。
"""
import json
import re
import shutil
import sqlite3
import threading
import time
from datetime import date, datetime
from pathlib import Path

import db

SHARD_LIMIT = 500          # 单目录图片上限,超过开 _002 分片
LIB_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
UNCLASSIFIED = "未分类"
THUMB_SIZE = 512

_lock = threading.Lock()   # 索引库串行写(收编线程 + 管理端点)


def library_dir() -> Path:
    """整理库根目录(设置 library_dir;默认 servershare 的 comfyui/library)。"""
    raw = (db.get_setting("library_dir") or "").strip()
    if raw:
        p = Path(raw).expanduser()
        if p.is_absolute():
            return p
    return Path.home() / "servershare" / "comfyui" / "library"


def output_dir() -> Path:
    """ComfyUI 落盘暂存区(收编来源;library 的同级 output)。"""
    return library_dir().parent / "output"


def nas_root() -> Path:
    """索引路径的基准根(comfyui 目录:library 与 output 的父级)。"""
    return library_dir().parent


def remote_enabled() -> bool:
    """library 是否处于本地盘之外的挂载点(决定读路径是否含 NAS 层)。"""
    want = str(library_dir())
    best = ""
    try:
        with open("/proc/mounts", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2:
                    mp = parts[1].replace("\\040", " ")
                    if mp != "/" and (want == mp or want.startswith(mp.rstrip("/") + "/")):
                        if len(mp) > len(best):
                            best = mp
    except OSError:
        return False
    return bool(best)


# ---------- 名字与路径规则 ----------

def _safe_name(s, limit=60):
    """任务名/批次主题/前缀 → 文件系统安全目录名(中文保留)。"""
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(s or "").strip())
    s = re.sub(r"\s+", " ", s).strip(" ._")
    return s[:limit] or "未命名"


def _model_stem(model):
    """anima_turboV11.safetensors → anima_turboV11。"""
    return _safe_name(str(model or "").rsplit(".", 1)[0] or "未知模型", 80)


def _prefix_of(filename):
    """batchgen_00123_.png → batchgen(未分类归组用)。"""
    stem = re.sub(r"_?\d+_?$", "", str(filename).rsplit(".", 1)[0])
    return _safe_name(stem or "misc", 60)


def _shard_dir(base: Path) -> Path:
    """base 不满则用 base;满了开 _002/_003…(只增不改名:已存在的目录永不重命名)。"""
    def count(d: Path) -> int:
        try:
            return sum(1 for p in d.iterdir() if p.is_file())
        except OSError:
            return 0
    if not base.exists() or count(base) < SHARD_LIMIT:
        return base
    n = 2
    while True:
        cand = base.parent / f"{base.name}_{n:03d}"
        if not cand.exists() or count(cand) < SHARD_LIMIT:
            return cand
        n += 1


def classify(filename, meta):
    """按归属规则给出 (相对 library 根的父目录, tags 列表)。

    meta: {model, category, batch, workflow, prompt, seed, created_at} 或 None(无任务归属)。
    """
    if meta and (meta.get("model") or meta.get("batch") or meta.get("workflow")):
        day = str(meta.get("created_at") or "")[:10] or date.today().isoformat()
        theme = _safe_name(meta.get("batch") or meta.get("workflow") or "生成")
        parent = Path(_model_stem(meta.get("model"))) / f"{day}_{theme}"
        tags = [t for t in (_model_stem(meta.get("model")), meta.get("category"),
                            meta.get("batch"), meta.get("workflow")) if t]
    else:
        prefix = _prefix_of(filename)
        parent = Path(UNCLASSIFIED) / prefix
        tags = [UNCLASSIFIED, prefix]
    return parent, tags


# ---------- 索引(本机正本;path 为主键,相对 nas_root) ----------

_FILES_DDL = """CREATE TABLE files(
    path TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    thumb TEXT,
    model TEXT DEFAULT '',
    category TEXT DEFAULT '',
    batch TEXT DEFAULT '',
    workflow TEXT DEFAULT '',
    prompt TEXT DEFAULT '',
    seed INTEGER,
    lora TEXT DEFAULT '',
    tags TEXT DEFAULT '[]',
    size INTEGER DEFAULT 0,
    source TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
)"""


def _migrate_v1(conn, lib_prefix):
    """v1(文件名主键、library 根相对路径)→ v2(path 主键、nas 根相对路径)。"""
    conn.executescript("ALTER TABLE files RENAME TO files_v1;")
    conn.execute(_FILES_DDL)
    conn.execute(
        f"""INSERT OR REPLACE INTO files(path, filename, thumb, model, category,
            batch, workflow, prompt, seed, lora, tags, size, source, created_at)
            SELECT '{lib_prefix}/'||path, filename,
                   CASE WHEN thumb THEN '{lib_prefix}/'||thumb END,
                   model, category, batch, workflow, prompt, seed, '', tags, size,
                   source, created_at
            FROM files_v1""")
    conn.execute("DROP TABLE files_v1")
    conn.commit()


def _lib_connect():
    conn = sqlite3.connect(db.DATA_DIR / "library.db", timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    info = list(conn.execute("PRAGMA table_info(files)"))
    pk_cols = {r[1] for r in info if r[5]}
    if info and "path" not in pk_cols:  # v1(path 非主键)→ v2
        _migrate_v1(conn, library_dir().name)
    elif not info:
        conn.execute(_FILES_DDL)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_created ON files(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_filename ON files(filename)")
    return conn


def indexed(filename):
    with _lock:
        row = _lib_connect().execute(
            "SELECT rowid, * FROM files WHERE filename=? LIMIT 1", (filename,)).fetchone()
    return dict(row) if row else None


def view_neighbors(rowid, where="", args=(), window=40):
    """详情页定位:当前行 + 全库(或筛选集)内位置 + 邻图窗口 + 跨窗口接续 ID。

    排序与画廊网格一致(created_at DESC, rowid DESC);pos 从最新数起。
    邻图只取当前 ±window,窗口外给出 newer_rid/older_rid 供前端跳转续览。
    """
    conn = _lib_connect()
    cur = conn.execute("SELECT rowid, * FROM files WHERE rowid=?", (rowid,)).fetchone()
    if not cur:
        conn.close()
        return None
    key = "(created_at, rowid)"

    def wcond(op):
        if where:
            return f"WHERE {where} AND {key} {op} (?, ?)", (*args, cur["created_at"], rowid)
        return f"WHERE {key} {op} (?, ?)", (cur["created_at"], rowid)

    w_total, a_total = (f"WHERE {where}", args) if where else ("", ())
    total = conn.execute(f"SELECT COUNT(*) FROM files {w_total}", a_total).fetchone()[0]
    w_gt, a_gt = wcond(">")
    pos = conn.execute(f"SELECT COUNT(*) FROM files {w_gt}", a_gt).fetchone()[0] + 1
    w_le, a_le = wcond("<=")   # 当前及更新(排序靠前)
    w_ge, a_ge = wcond(">=")   # 当前及更旧(排序靠后)
    newer = conn.execute(
        f"SELECT rowid, * FROM files {w_le} ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (*a_le, window + 1)).fetchall()
    older = conn.execute(
        f"SELECT rowid, * FROM files {w_ge} ORDER BY created_at ASC, rowid ASC LIMIT ?",
        (*a_ge, window + 1)).fetchall()
    conn.close()
    newer_rid = newer[-1]["rowid"] if len(newer) > window else None
    older_rid = older[-1]["rowid"] if len(older) > window else None
    neighbors = [dict(r) for r in list(newer[:window]) + list(reversed(older[:window]))]
    return {"row": dict(cur), "pos": pos, "total": total, "neighbors": neighbors,
            "newer_rid": newer_rid, "older_rid": older_rid}


def get(rowid):
    with _lock:
        row = _lib_connect().execute(
            "SELECT rowid, * FROM files WHERE rowid=?", (rowid,)).fetchone()
    return dict(row) if row else None


def _index_add(path, filename, thumb, model, category, batch, workflow,
               prompt, seed, lora, tags, size, source, created_at=None):
    with _lock:
        conn = _lib_connect()
        conn.execute(
            "INSERT OR REPLACE INTO files(path, filename, thumb, model, category, batch, "
            "workflow, prompt, seed, lora, tags, size, source, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?, COALESCE(?, datetime('now','localtime')))",
            (str(path), filename, thumb, model or "", category or "", batch or "",
             workflow or "", prompt or "", seed, lora or "",
             json.dumps(tags, ensure_ascii=False), size, source, created_at))
        conn.commit()
        conn.close()


def _index_remove(filename):
    with _lock:
        conn = _lib_connect()
        conn.execute("DELETE FROM files WHERE filename=?", (filename,))
        conn.commit()
        conn.close()


def find_on_nas(filename, subfolder="", img_type="output"):
    """/image 读路径用:索引里的 NAS 路径,或 output/ 根的未收编原位文件。"""
    row = indexed(filename)
    if row:
        p = nas_root() / row["path"]
        try:
            if p.exists():
                return p
        except OSError:
            pass
    try:
        p = output_dir() / _safe_name(filename, 200)
        if p.is_file():
            return p
    except OSError:
        pass
    return None


# ---------- 缩略图(Pillow 本地生成 → library/thumbs 平行树) ----------

def thumb_generate(row):
    """为索引行生成缩略图,返回 bytes 或 None;成功则更新索引。"""
    try:
        from PIL import Image
        src = nas_root() / row["path"]
        with Image.open(src) as img:
            img = img.convert("RGB")
            img.thumbnail((THUMB_SIZE, THUMB_SIZE))
            import io
            buf = io.BytesIO()
            img.save(buf, "WEBP", quality=75)
            body = buf.getvalue()
        thumb_rel = Path(library_dir().name) / "thumbs" / Path(row["path"]).with_suffix(".webp")
        tdest = nas_root() / thumb_rel
        tdest.parent.mkdir(parents=True, exist_ok=True)
        tdest.write_bytes(body)
        with _lock:
            conn = _lib_connect()
            conn.execute("UPDATE files SET thumb=? WHERE path=?", (str(thumb_rel), row["path"]))
            conn.commit()
            conn.close()
        return body
    except Exception as e:
        print(f"缩略图生成失败 {row.get('filename')}: {e}")
        return None


def thumb_sweep(limit=6):
    """补齐缺缩略图的行(后台线程低频调用)。返回生成数。"""
    with _lock:
        conn = _lib_connect()
        rows = conn.execute(
            "SELECT rowid, * FROM files WHERE thumb IS NULL LIMIT ?", (limit,)).fetchall()
        conn.close()
    done = 0
    for r in rows:
        if thumb_generate(dict(r)):
            done += 1
    return done


# ---------- 全库摄取(output/ 手工子目录原地索引,不移动文件) ----------

def ingest_refresh():
    """扫描 output/ 全部子目录,把新/变更图片索引进来(不移动)。

    有任务记录的文件名顺带补全元数据;全部无变更时零写入。返回新增/更新数。
    """
    out = output_dir()
    changed = 0
    try:
        with _lock:
            conn = _lib_connect()
            like = output_dir().name + "/%"
            known = {r["path"]: (r["size"], r["created_at"]) for r in conn.execute(
                "SELECT path, size, created_at FROM files WHERE path LIKE ?", (like,))}
            conn.close()
        for sub in sorted(p for p in out.iterdir() if p.is_dir()):
            for f in sub.rglob("*"):
                if not f.is_file() or f.suffix.lower() not in LIB_EXTENSIONS:
                    continue
                rel = f.relative_to(nas_root())
                try:
                    st = f.stat()
                except OSError:
                    continue
                ctime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                if str(rel) in known and known[str(rel)] == (st.st_size, ctime):
                    continue
                meta = lookup_task_meta(f.name)
                dirname = sub.name
                if meta:
                    model = meta.get("model") or dirname
                    tags = [t for t in (dirname, meta.get("category"),
                                        meta.get("batch"), meta.get("workflow")) if t]
                else:
                    model, tags = dirname, [dirname]
                _index_add(rel, f.name, None, model, meta.get("category") if meta else "",
                           meta.get("batch") if meta else "",
                           meta.get("workflow") if meta else "",
                           meta.get("prompt") if meta else "",
                           meta.get("seed") if meta else "",
                           meta.get("lora") if meta else "",
                           tags, st.st_size, "ingest", ctime)
                changed += 1
    except OSError as e:
        print(f"全库摄取失败: {e}")
    return changed


# ---------- 画廊查询 ----------

def page_query(where, args, page, per_page):
    """分页查询(画廊数据源)。where 为已参数化的条件子串(可为空)。"""
    base = "FROM files"
    if where:
        base += " WHERE " + where
    with _lock:
        conn = _lib_connect()
        total = conn.execute(f"SELECT COUNT(*) {base}", args).fetchone()[0]
        rows = conn.execute(
            f"SELECT rowid, * {base} ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
            (*args, per_page, (page - 1) * per_page)).fetchall()
        conn.close()
    return [dict(r) for r in rows], total


def filter_options():
    """筛选下拉的可选项(模型/工作流/类型/批次/LoRA)。"""
    with _lock:
        conn = _lib_connect()

        def col(name):
            return [r[0] for r in conn.execute(
                f"SELECT DISTINCT {name} FROM files WHERE {name}!='' ORDER BY 1")]
        opts = {k: col(k) for k in ("model", "workflow", "category", "batch", "lora")}
        conn.close()
    return opts


def lookup_task_meta(filename):
    """按文件名在 comfyweb 库里找最近一条任务归属(images→tasks)。"""
    row = db.query_one(
        "SELECT t.model, t.category, t.batch, t.workflow_name, t.prompt_text, "
        "t.seed, t.lora, t.created_at FROM images i JOIN tasks t ON t.id=i.task_id "
        "WHERE i.filename=? AND i.type='output' ORDER BY i.id DESC LIMIT 1",
        (filename,))
    if not row:
        return None
    m = dict(row)
    m["workflow"] = m.pop("workflow_name", "") or ""
    m["prompt"] = m.pop("prompt_text", "") or ""
    return m


def task_img_id_map(filenames):
    """文件名 → {img_id, fav}(画廊卡片链接旧详情页与收藏角标用;一次查询)。"""
    names = [f for f in filenames if f]
    if not names:
        return {}
    marks = ",".join("?" * len(names))
    rows = db.query(
        f"SELECT filename, MIN(id) AS img_id, MAX(fav) AS fav FROM images "
        f"WHERE filename IN ({marks}) GROUP BY filename", names)
    return {r["filename"]: {"img_id": r["img_id"], "fav": bool(r["fav"])} for r in rows}


def place(local_path: Path, filename, source="archive"):
    """把本地已有文件放入 library 树(分类 + 复制 + 缩略图延后 + 索引)。

    library 已有不低于本地质量的副本时跳过复制,只补索引。返回相对 nas_root 的路径。
    """
    meta = lookup_task_meta(filename)
    parent, tags = classify(filename, meta)
    dest_dir = _shard_dir(library_dir() / parent)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / _safe_name(filename, 200)
    size = local_path.stat().st_size
    # 目标已有不低于本地质量的副本时不覆盖(防缩略图替身降级覆盖全画质原图)
    if not (dest.exists() and dest.stat().st_size >= size):
        shutil.copyfile(local_path, dest)
    rel = Path(library_dir().name) / dest.relative_to(library_dir())
    _index_add(rel, filename, None,
               meta.get("model") if meta else "", meta.get("category") if meta else "",
               meta.get("batch") if meta else "", meta.get("workflow") if meta else "",
               meta.get("prompt") if meta else "", meta.get("seed") if meta else "",
               meta.get("lora") if meta else "", tags, size, source)
    return rel


def collect_once(limit=50):
    """收编 output/ 根目录平铺文件(不碰子目录):移动进库 + 索引。

    返回 (收编数, 跳过数)。已入索引的文件名跳过(GPU 同步重复推送防护)。
    """
    out = output_dir()
    moved = skipped = 0
    try:
        entries = sorted(p for p in out.iterdir()
                         if p.is_file() and p.suffix.lower() in LIB_EXTENSIONS)
    except OSError:
        return 0, 0
    for p in entries:
        if moved >= limit:
            break
        if indexed(p.name):
            skipped += 1
            continue
        meta = lookup_task_meta(p.name)
        parent, tags = classify(p.name, meta)
        dest_dir = _shard_dir(library_dir() / parent)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / _safe_name(p.name, 200)
        try:
            shutil.move(str(p), str(dest))  # NAS 内移动,秒级
        except OSError as e:
            print(f"收编移动失败 {p.name}: {e}")
            continue
        rel = Path(library_dir().name) / dest.relative_to(library_dir())
        _index_add(rel, p.name, None,
                   meta.get("model") if meta else "", meta.get("category") if meta else "",
                   meta.get("batch") if meta else "", meta.get("workflow") if meta else "",
                   meta.get("prompt") if meta else "", meta.get("seed") if meta else "",
                   meta.get("lora") if meta else "", tags,
                   dest.stat().st_size, "collect")
        moved += 1
    return moved, skipped


def snapshot():
    """索引快照推送 NAS:backup API 出本地成品 → 字节拷贝 + md5 校验。"""
    import hashlib
    src = db.DATA_DIR / "library.db"
    if not src.exists():
        return False
    tmp = db.DATA_DIR / "library-snapshot.db"
    s = sqlite3.connect(src)
    d = sqlite3.connect(tmp)
    with d:
        s.backup(d)
    d.close()
    s.close()
    dest = library_dir() / "index.db"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(tmp, dest)
    h = lambda p: hashlib.md5(Path(p).read_bytes()).hexdigest()
    ok = h(tmp) == h(dest)
    tmp.unlink(missing_ok=True)
    if not ok:
        raise OSError("索引快照 md5 校验不一致")
    return True


def reverse(filename):
    """回滚:把已收编的图还原回 output/ 根并删除索引行(含缩略图)。"""
    row = indexed(filename)
    if not row:
        return False
    lib_file = nas_root() / row["path"]
    out = output_dir() / filename
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(lib_file), str(out))
    if row["thumb"]:
        (nas_root() / row["thumb"]).unlink(missing_ok=True)
    _index_remove(filename)
    return True


def status():
    """设置页展示:索引张数/未分类/已摄取/待缩略图/最后快照时间/待收编数。"""
    n = un = ing = nothumb = 0
    last = ""
    try:
        with _lock:
            conn = _lib_connect()
            n = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            lib_like = library_dir().name + "/%"
            un = conn.execute(
                "SELECT COUNT(*) FROM files WHERE path LIKE ? AND path LIKE '%/未分类/%'",
                (lib_like,)).fetchone()[0]
            ing = conn.execute(
                "SELECT COUNT(*) FROM files WHERE path LIKE ?",
                (output_dir().name + "/%",)).fetchone()[0]
            nothumb = conn.execute(
                "SELECT COUNT(*) FROM files WHERE thumb IS NULL").fetchone()[0]
            conn.close()
    except Exception:
        pass
    try:
        idx = library_dir() / "index.db"
        if idx.exists():
            last = time.strftime("%m-%d %H:%M", time.localtime(idx.stat().st_mtime))
    except OSError:
        pass
    pending = 0
    try:
        pending = sum(1 for p in output_dir().iterdir()
                      if p.is_file() and p.suffix.lower() in LIB_EXTENSIONS
                      and not indexed(p.name))
    except OSError:
        pass
    return {"indexed": n, "unclassified": un, "ingested": ing, "no_thumb": nothumb,
            "last_snapshot": last, "pending": pending}
