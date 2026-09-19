"""NAS 图片库(library/):分类树 + 索引 + 缩略图 + 收编。

目录规则(经确认的架构决策):
- 大类 = 模型名(tasks.model 去扩展名);有任务归属的图进 `模型/日期_批次或工作流/`,
  无归属的进 `未分类/<原前缀>/`(保留以后归位的线索);
- 分片"只增不改名":目录满 SHARD_LIMIT 张即封存,后续新图写入 _002 分片,
  已入索引的路径永不改变——索引因此永不失效,也不影响正在生成的图;
- 收编:扫描 output/ 根目录平铺文件(手工子目录一律不碰),能对上 comfyweb
  任务记录的按记录归位,对不上的按前缀归未分类;GPU 同步重复推送的文件名
  以索引为准跳过;
- 索引正本在本机 data/library.db(SQLite 绝不在 CIFS 上开写连接,实测锁死),
  每轮收编后经 backup API 快照 + 字节拷贝 + md5 校验推送 NAS index.db;
- reverse(filename) 把已收编的图还原回 output/ 并删索引行,作回滚保底。
"""
import json
import re
import shutil
import sqlite3
import threading
import time
from datetime import date
from pathlib import Path

import db

SHARD_LIMIT = 500          # 单目录图片上限,超过开 _002 分片
LIB_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
UNCLASSIFIED = "未分类"

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
    """按归属规则给出 (相对路径父目录, tags 列表)。

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


# ---------- 索引(本机正本) ----------

def _lib_connect():
    conn = sqlite3.connect(db.DATA_DIR / "library.db", timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS files(
        filename TEXT PRIMARY KEY,
        path TEXT NOT NULL,
        thumb TEXT,
        model TEXT DEFAULT '',
        category TEXT DEFAULT '',
        batch TEXT DEFAULT '',
        workflow TEXT DEFAULT '',
        prompt TEXT DEFAULT '',
        seed INTEGER,
        tags TEXT DEFAULT '[]',
        size INTEGER DEFAULT 0,
        source TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now','localtime'))
    )""")
    return conn


def indexed(filename):
    with _lock:
        row = _lib_connect().execute(
            "SELECT * FROM files WHERE filename=?", (filename,)).fetchone()
    return dict(row) if row else None


def _index_add(filename, rel_path, thumb, meta, tags, size, source):
    with _lock:
        conn = _lib_connect()
        conn.execute(
            "INSERT OR REPLACE INTO files(filename,path,thumb,model,category,batch,"
            "workflow,prompt,seed,tags,size,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (filename, str(rel_path), thumb,
             (meta or {}).get("model", ""), (meta or {}).get("category", ""),
             (meta or {}).get("batch", ""), (meta.get("workflow") if meta else "") or "",
             (meta or {}).get("prompt", ""), (meta or {}).get("seed"),
             json.dumps(tags, ensure_ascii=False), size, source))
        conn.commit()
        conn.close()


def _index_remove(filename):
    with _lock:
        conn = _lib_connect()
        conn.execute("DELETE FROM files WHERE filename=?", (filename,))
        conn.commit()
        conn.close()


def find_on_nas(filename, subfolder="", img_type="output"):
    """读路径用:索引里的 NAS library 路径,或 output/ 根的未收编原位文件。"""
    row = indexed(filename)
    if row:
        p = library_dir() / row["path"]
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


# ---------- 缩略图(三级:本地缓存 → ComfyUI preview → NULL) ----------

def thumb_for(filename, subfolder="", img_type="output"):
    """返回 (bytes, 扩展名) 或 None。优先本机缩略图缓存,再试 ComfyUI preview。"""
    from views.helpers import img_cache_get
    for preview in ("webp;jpeg;70", "webp;jpeg;75"):
        bp, _ct = img_cache_get(f"img:{img_type}:{subfolder}:{filename}:{preview}")
        if bp:
            try:
                return bp.read_bytes(), ".webp"
            except OSError:
                continue
    from comfy_client import ComfyError, client
    try:
        r = client.download_image(filename, subfolder, img_type, "webp;jpeg;70")
    except Exception:
        return None
    body = r.content
    r.close()
    return body, ".webp"


# ---------- 收编与放置 ----------

def lookup_task_meta(filename):
    """按文件名在 comfyweb 库里找最近一条任务归属(images→tasks)。"""
    row = db.query_one(
        "SELECT t.model, t.category, t.batch, t.workflow_name, t.prompt_text, "
        "t.seed, t.created_at FROM images i JOIN tasks t ON t.id=i.task_id "
        "WHERE i.filename=? AND i.type='output' ORDER BY i.id DESC LIMIT 1",
        (filename,))
    return dict(row) if row else None


def place(local_path: Path, filename, source="archive"):
    """把本地已有文件放入 library 树(分类 + 复制 + 缩略图 + 索引)。

    library 已有同名同尺寸文件时跳过复制,只补索引。返回相对路径。
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
    rel = dest.relative_to(library_dir())
    thumb_rel = None
    th = thumb_for(filename)
    if th:
        tdest = library_dir() / "thumbs" / rel.parent / (dest.stem + th[1])
        tdest.parent.mkdir(parents=True, exist_ok=True)
        if not tdest.exists():
            tdest.write_bytes(th[0])
        thumb_rel = str(tdest.relative_to(library_dir()))
    _index_add(filename, rel, thumb_rel, meta, tags, size, source)
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
        rel = dest.relative_to(library_dir())
        thumb_rel = None
        th = thumb_for(p.name)
        if th:
            tdest = library_dir() / "thumbs" / rel.parent / (dest.stem + th[1])
            tdest.parent.mkdir(parents=True, exist_ok=True)
            if not tdest.exists():
                try:
                    tdest.write_bytes(th[0])
                except OSError:
                    thumb_rel = None
            if tdest.exists():
                thumb_rel = str(tdest.relative_to(library_dir()))
        _index_add(p.name, rel, thumb_rel, meta, tags,
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
    lib_file = library_dir() / row["path"]
    out = output_dir() / filename
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(lib_file), str(out))
    if row["thumb"]:
        (library_dir() / row["thumb"]).unlink(missing_ok=True)
    _index_remove(filename)
    return True


def status():
    """设置页展示:索引张数/未分类数/最后快照时间/待收编数。"""
    n = un = 0
    last = ""
    try:
        with _lock:
            conn = _lib_connect()
            n = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            un = conn.execute(
                "SELECT COUNT(*) FROM files WHERE path LIKE '未分类/%'").fetchone()[0]
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
    return {"indexed": n, "unclassified": un, "last_snapshot": last,
            "pending": pending}
