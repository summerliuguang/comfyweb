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
import logging
import re
import shutil
import sqlite3
import threading
import time
from datetime import date, datetime
from pathlib import Path

import db

log = logging.getLogger("comfyweb")

SHARD_LIMIT = 500          # 单目录图片上限,超过开 _002 分片
LIB_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
UNCLASSIFIED = "未分类"
THUMB_SIZE = 512

_lock = threading.Lock()   # 索引库串行写(收编线程 + 管理端点)


def library_dir() -> Path:
    """整理库根目录(设置 library_dir;默认本地 data/library,可改 NAS/网络路径)。"""
    raw = (db.get_setting("library_dir") or "").strip()
    if raw:
        p = Path(raw).expanduser()
        if p.is_absolute():
            return p
    return db.DATA_DIR / "library"


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

    if meta and meta.get("nsfw"):
        parent = f"nsfw/{parent}"   # 私密内容独立子树(同分片规则)
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
    created_at TEXT DEFAULT (datetime('now','localtime')),
    params_json TEXT
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


_lib_conn = None


def _lib_connect():
    """单例连接(check_same_thread=False):所有访问都持 _lock 串行,跨线程安全。
    WAL PRAGMA/迁移探测/DDL 只在首建时执行——此前每次调用都重跑一遍,而本函数
    被用在逐文件循环里(storage 同步/收编/status),开销被放大成每张图一次"建连+DDL"。"""
    global _lib_conn
    if _lib_conn is None:
        conn = sqlite3.connect(db.DATA_DIR / "library.db", timeout=15,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        info = list(conn.execute("PRAGMA table_info(files)"))
        pk_cols = {r[1] for r in info if r[5]}
        if info and "path" not in pk_cols:  # v1(path 非主键)→ v2
            _migrate_v1(conn, library_dir().name)
        elif not info:
            conn.execute(_FILES_DDL)
        # 建表/迁移后按实际结构补列(旧库自动加 params_json)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(files)")}
        if "params_json" not in cols:
            conn.execute("ALTER TABLE files ADD COLUMN params_json TEXT")
        if "nsfw" not in cols:
            conn.execute("ALTER TABLE files ADD COLUMN nsfw INTEGER NOT NULL DEFAULT 0")
        if "pre_nsfw_path" not in cols:
            conn.execute("ALTER TABLE files ADD COLUMN pre_nsfw_path TEXT")
        conn.execute("UPDATE files SET seed=NULL WHERE seed=''")  # 旧写入把空串塞进过 INTEGER 列
        conn.execute("UPDATE files SET model='' WHERE model!='' AND "
                     "model NOT LIKE '%.safetensors' AND model NOT LIKE '%.ckpt' "
                     "AND model NOT LIKE '%.pt' AND model NOT LIKE '%.sft'")  # 手工目录名曾被误存为模型
        conn.execute("UPDATE files SET pre_nsfw_path=REPLACE(path, 'library/nsfw/', 'library/') "
                     "WHERE nsfw=1 AND (pre_nsfw_path IS NULL OR pre_nsfw_path='') "
                     "AND path LIKE 'library/nsfw/%'")
        conn.commit()
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_created ON files(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_filename ON files(filename)")
        conn.execute("""CREATE TABLE IF NOT EXISTS deleted_files(
            filename TEXT PRIMARY KEY,
            deleted_at TEXT DEFAULT (datetime('now','localtime')))""")
        _lib_conn = conn
    return _lib_conn


def reset_conn():
    """关闭并丢弃单例连接(测试隔离/库文件重建后调用;下次访问自动重开)。"""
    global _lib_conn
    with _lock:
        if _lib_conn is not None:
            try:
                _lib_conn.close()
            except sqlite3.Error:
                pass
        _lib_conn = None


def indexed(filename):
    with _lock:
        row = _lib_connect().execute(
            "SELECT rowid, * FROM files WHERE filename=? LIMIT 1", (filename,)).fetchone()
    return dict(row) if row else None


def indexed_map(filenames):
    """批量按文件名查询(同步线程整轮一次,替代逐文件单查)。"""
    filenames = [f for f in filenames if f]
    if not filenames:
        return {}
    marks = ",".join("?" * len(filenames))
    with _lock:
        rows = _lib_connect().execute(
            f"SELECT rowid, * FROM files WHERE filename IN ({marks})", filenames).fetchall()
    return {r["filename"]: dict(r) for r in rows}


def view_neighbors(rowid, where="", args=(), window=40):
    """详情页定位:当前行 + 全库(或筛选集)内位置 + 邻图窗口 + 跨窗口接续 ID。

    排序与画廊网格一致(created_at DESC, rowid DESC);pos 从最新数起。
    邻图只取当前 ±window,窗口外给出 newer_rid/older_rid 供前端跳转续览。
    """
    with _lock:
        conn = _lib_connect()
        cur = conn.execute("SELECT rowid, * FROM files WHERE rowid=?", (rowid,)).fetchone()
        if not cur:
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
        # 更新方向(不含自己)在前,更旧方向(不含自己)在后,自己插中间——
        # 此前两向比较符写反(<=给了newer/>=给了older),自己出现两次且滑动方向错乱
        w_n, a_n = wcond(">")
        newer = conn.execute(
            f"SELECT rowid, * FROM files {w_n} ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (*a_n, window + 1)).fetchall()
        w_o, a_o = wcond("<")
        older = conn.execute(
            f"SELECT rowid, * FROM files {w_o} ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (*a_o, window + 1)).fetchall()
        newer_rid = newer[-1]["rowid"] if len(newer) > window else None
        older_rid = older[-1]["rowid"] if len(older) > window else None
        newer_w = [dict(r) for r in newer[:window]]      # DESC:从最新到紧邻自己
        older_w = [dict(r) for r in older[:window]]      # DESC:紧邻自己在前(滑动顺序)
        neighbors = newer_w + [dict(cur)] + older_w
    return {"row": dict(cur), "pos": pos, "total": total, "neighbors": neighbors,
            "idx": len(newer_w), "newer_rid": newer_rid, "older_rid": older_rid}


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


def _index_remove(filename):
    with _lock:
        conn = _lib_connect()
        conn.execute("DELETE FROM files WHERE filename=?", (filename,))
        conn.commit()


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
        return body
    except Exception as e:
        log.warning("缩略图生成失败 %s: %s", row.get("filename"), e)
        return None


def thumb_sweep(limit=6):
    """补齐缺缩略图的行(后台线程低频调用)。返回生成数。"""
    with _lock:
        conn = _lib_connect()
        rows = conn.execute(
            "SELECT rowid, * FROM files WHERE thumb IS NULL LIMIT ?", (limit,)).fetchall()
    done = 0
    for r in rows:
        if thumb_generate(dict(r)):
            done += 1
    return done


# ---------- 图片内嵌参数(ComfyUI PNG tEXt 'prompt' 块,API 格式) ----------
# 非 comfyweb 生成的图没有任务记录,但 ComfyUI 落盘时把完整工作流写进了 PNG,
# 从这里把提示词/seed/采样器等补进索引,详情页才能对无记录图显示参数。

def _chunk_text(value, nodes):
    """解引用 API 格式输入:引用形如 [node_id, output_slot],逐层追到字符串。"""
    seen = 0
    while isinstance(value, list) and len(value) == 2 and seen < 10:
        node = nodes.get(str(value[0]))
        if not node:
            return None
        ins = node.get("inputs", {})
        for k in ("text", "value", "string"):
            if k in ins:
                value = ins[k]
                break
        else:
            return None
        seen += 1
    return value.strip() if isinstance(value, str) else None


NEG_HINTS = ("worst quality", "low quality", "bad quality", "bad anatomy",
             "negative embedding", "bad hand")


def _conditioning_text(ref, nodes):
    """从采样器的 positive/negative 引用出发,穿透 conditioning 包装节点
    (PerpNeg/ConditioningCombine 等),找到链上第一个 CLIPTextEncode 的文本。"""
    seen = set()
    stack = [ref]
    while stack:
        cur = stack.pop()
        if not (isinstance(cur, list) and len(cur) == 2):
            continue
        key = str(cur[0])
        if key in seen:
            continue
        seen.add(key)
        node = nodes.get(key)
        if not node:
            continue
        ins = node.get("inputs", {})
        if node.get("class_type") == "CLIPTextEncode":
            return _chunk_text(ins.get("text"), nodes)
        for v in ins.values():   # 包装节点:继续追所有节点引用
            if isinstance(v, list) and len(v) == 2:
                stack.append(v)
    return None


def parse_png_params(path):
    """从 PNG 内嵌 'prompt' 块解析生成参数,返回 dict 或 None(无块/损坏)。
    只读文件头(tEXt 在像素数据之前),不解码像素,CIFS 上也快。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            raw = im.info.get("prompt")
    except Exception:
        return None
    if not raw:
        return None
    try:
        nodes = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(nodes, dict) or not nodes:
        return None
    out = {}
    clip_texts = []
    for node in nodes.values():
        ct = node.get("class_type", "")
        ins = node.get("inputs", {})
        if "sampler_name" in ins or "noise_seed" in ins or ct.startswith("KSampler"):
            if isinstance(ins.get("sampler_name"), str) and ins["sampler_name"]:
                out.setdefault("sampler", ins["sampler_name"])
            for key in ("scheduler", "steps", "cfg", "denoise"):
                if isinstance(ins.get(key), (int, float, str)) and ins[key] != "":
                    out.setdefault(key, ins[key])
            for sk in ("seed", "noise_seed"):
                if isinstance(ins.get(sk), int):
                    out.setdefault("seed", ins[sk])
            for role in ("positive", "negative"):
                ref = ins.get(role)
                if isinstance(ref, list):
                    text = _conditioning_text(ref, nodes)
                    if text:
                        out.setdefault(role, text)
        if ct == "CLIPTextEncode":
            t = _chunk_text(ins.get("text"), nodes)
            if t:
                clip_texts.append(t)
        # 底模加载器:CheckpointLoader* 用 ckpt_name,UNETLoader 系(flux/z-image 等)
        # 用 unet_name;UpscaleModelLoader 等的 model_name 是放大模型,不当作底模
        ct_l = ct.lower()
        if "loader" in ct_l:
            if ct_l.startswith("checkpointloader") and ins.get("ckpt_name"):
                out.setdefault("model", ins["ckpt_name"])
            elif "unet" in ct_l and ins.get("unet_name"):
                out.setdefault("model", ins["unet_name"])
        if ins.get("lora_name"):
            out.setdefault("lora", ins["lora_name"])
    # 引用链没追到文本时(复杂图结构),拿最长的非负面特征 CLIP 文本兜底——
    # 负面提示词(如 "worst quality, low quality, ...")往往比正面还长,必须先排除
    if "positive" not in out and clip_texts:
        plain = [t for t in clip_texts
                 if not t.lower().startswith(NEG_HINTS)]
        if plain:
            out["positive"] = max(plain, key=len)
    out = {k: v for k, v in out.items() if v not in (None, "")}
    return out or None


def params_backfill(row):
    """解析单行图片的内嵌参数并回填索引。返回 True 表示行有写入。"""
    params = parse_png_params(nas_root() / row["path"])
    with _lock:
        conn = _lib_connect()
        if not params:
            # 写空串作"已扫无参数"标记,避免每轮重扫;已有 prompt/seed 不覆盖
            conn.execute("UPDATE files SET params_json='' WHERE path=?", (row["path"],))
            conn.commit()
            return False
        conn.execute(
            "UPDATE files SET params_json=?, "
            "prompt=CASE WHEN IFNULL(prompt,'')='' THEN ? ELSE prompt END, "
            "seed=CASE WHEN COALESCE(seed,'')='' THEN ? ELSE seed END, "
            "model=CASE WHEN IFNULL(model,'')='' THEN ? ELSE model END, "
            "lora=CASE WHEN IFNULL(lora,'')='' THEN ? ELSE lora END WHERE path=?",
            (json.dumps(params, ensure_ascii=False),
             params.get("positive") or "", params.get("seed"),
             params.get("model") or "", params.get("lora") or "", row["path"]))
        conn.commit()
    return True


def params_sweep(limit=20):
    """从图片内嵌参数补全索引(后台线程低频调用;PNG 文件头读取,单张毫秒级)。"""
    with _lock:
        conn = _lib_connect()
        rows = conn.execute(
            "SELECT rowid, * FROM files WHERE params_json IS NULL LIMIT ?", (limit,)).fetchall()
    done = 0
    for r in rows:
        try:
            if params_backfill(dict(r)):
                done += 1
        except Exception as e:
            log.warning("参数提取失败 %s: %s", r["filename"], e)
    return done


def delete_image(rowid):
    """彻底删除图片:正本+缩略图+本地缓冲+索引行+任务图片记录,并记墓碑
    防止 GPU 同步/本地缓冲补同步/收编/补拉把图重新带回。正本在 output 手工
    子目录的(原地索引未移动)删的就是 output 里的文件。tasks 行不动——
    同任务多图与"再次生成"不受影响。返回 (ok, error)。"""
    row = get(rowid)
    if not row:
        return False, "图片不存在"
    filename = row["filename"]
    try:
        target = nas_root() / row["path"]
        target.unlink(missing_ok=True)
        if row["thumb"]:
            (nas_root() / row["thumb"]).unlink(missing_ok=True)
    except OSError as e:
        return False, f"文件删除失败(NAS 不可用?): {e.__class__.__name__}"
    import storage
    storage.remove_local(filename)   # 本地缓冲副本是复活源,一并清掉
    with _lock:
        conn = _lib_connect()
        conn.execute("INSERT OR REPLACE INTO deleted_files(filename) VALUES(?)", (filename,))
        conn.execute("DELETE FROM files WHERE rowid=?", (rowid,))
        conn.commit()
    db.execute("DELETE FROM images WHERE filename=?", (filename,))
    return True, ""


def private_candidates_by_workflow(workflow_id):
    """工作流的历史图片中尚未标私密的 rowid 列表(标私密时自动联动迁移)。
    images/tasks 在 comfyweb.db、files 在 library.db,两步查询不可跨库 JOIN。"""
    names = [r["filename"] for r in db.query(
        "SELECT DISTINCT i.filename FROM images i JOIN tasks t ON t.id=i.task_id "
        "WHERE t.workflow_id=? AND i.type='output'", (workflow_id,))]
    if not names:
        return []
    marks = ",".join("?" * len(names))
    with _lock:
        conn = _lib_connect()
        rows = conn.execute(
            f"SELECT rowid FROM files WHERE nsfw=0 AND filename IN ({marks})",
            names).fetchall()
    return [r[0] for r in rows]


def migrate_to_private(rowids):
    """批量移入私密区(后台线程调用;NAS 内 rename 毫秒级)。返回成功数。"""
    done = 0
    for rid in rowids:
        ok, _err = mark_private(rid, True)
        if ok:
            done += 1
        else:
            time.sleep(0.2)   # 失败(多为 NAS 抖动)稍缓再继续下一张
    if done:
        log.info("私密化迁移: %d 张图片移入 nsfw/ 区", done)
    return done


def _thumb_rel_for(path_rel):
    """path(library/x/f.png 或 output/i/f.png)→ 对应缩略图相对路径。"""
    inner = Path(path_rel)
    if inner.parts and inner.parts[0] == Path(library_dir().name):
        inner = Path(*inner.parts[1:])
    return Path(library_dir().name) / "thumbs" / inner.with_suffix(".webp")


def mark_private(rowid, nsfw):
    """单张图片私密化/取消:文件与缩略图在 nsfw/ 子树与原位置之间移动,索引同步。

    nsfw 树路径 = library/nsfw/<原完整 path>,pre_nsfw_path 记录原位置,取消时精确
    还原——output 手工子目录的图也会被搬进私密区并在取消时回到原手工目录。
    返回 (ok, error)。
    """
    row = get(rowid)
    if not row:
        return False, "图片不存在"
    old_rel = Path(row["path"])
    lib_name = Path(library_dir().name)
    in_nsfw = bool(row["nsfw"])
    if nsfw == in_nsfw:
        return True, ""
    try:
        if nsfw:
            new_rel = Path(lib_name) / "nsfw" / old_rel
            new_thumb_rel = Path(lib_name) / "thumbs" / "nsfw" / old_rel.with_suffix(".webp")
            pre = str(old_rel)
        else:
            pre = row["pre_nsfw_path"] or ""
            if pre:
                new_rel = Path(pre)
                new_thumb_rel = _thumb_rel_for(new_rel)
            else:   # 无原位置记录(如路径规范变更):按分类规则重算
                meta = lookup_task_meta(row["filename"])
                parent, _tags = classify(row["filename"], meta)
                new_rel = Path(lib_name) / parent / row["filename"]
                new_thumb_rel = Path(lib_name) / "thumbs" / parent / (row["filename"] + ".webp")
        src_file = nas_root() / old_rel
        dest = nas_root() / new_rel
        if dest.exists():
            if dest.stat().st_size == src_file.stat().st_size:
                src_file.unlink()
            else:
                return False, "目标位置存在同名不同内容的图片"
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src_file), str(dest))
        old_thumb = (nas_root() / row["thumb"]) if row["thumb"] else None
        if old_thumb and old_thumb.exists():
            nt = nas_root() / new_thumb_rel
            nt.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old_thumb), str(nt))
            thumb_val = str(new_thumb_rel)
        else:
            thumb_val = ""
        with _lock:
            conn = _lib_connect()
            if nsfw:
                conn.execute(
                    "UPDATE files SET path=?, thumb=?, nsfw=1, pre_nsfw_path=? WHERE rowid=?",
                    (str(new_rel), thumb_val, pre, rowid))
            else:
                conn.execute(
                    "UPDATE files SET path=?, thumb=?, nsfw=0, pre_nsfw_path='' WHERE rowid=?",
                    (str(new_rel), thumb_val, rowid))
            conn.commit()
    except OSError as e:
        return False, f"移动失败(NAS 不可用?): {e.__class__.__name__}"
    try:
        import storage
        storage.note_dirty()
    except Exception:
        pass
    return True, ""


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
        for sub in sorted(p for p in out.iterdir() if p.is_dir()):
            for f in sub.rglob("*"):
                if not f.is_file() or f.suffix.lower() not in LIB_EXTENSIONS:
                    continue
                if _is_tombstoned(f.name):
                    continue   # 已删除过的图不重新索引(不删,手工子目录是用户自留地)
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
                    model, tags = "", [dirname]   # 目录名不是模型,只留作标签
                _index_add(rel, f.name, None, model, meta.get("category") if meta else "",
                           meta.get("batch") if meta else "",
                           meta.get("workflow") if meta else "",
                           meta.get("prompt") if meta else "",
                           meta.get("seed") if meta else None,
                           meta.get("lora") if meta else "",
                           tags, st.st_size, "ingest", ctime)
                changed += 1
    except OSError as e:
        log.warning("全库摄取失败: %s", e)
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
    return [dict(r) for r in rows], total


def filter_options():
    """筛选下拉的可选项(模型/工作流/类型/批次/LoRA)。"""
    with _lock:
        conn = _lib_connect()

        hide = "" if (db.get_setting("private_enabled") or "") == "1" else " AND nsfw=0"

        def col(name):
            return [r[0] for r in conn.execute(
                f"SELECT DISTINCT {name} FROM files WHERE {name}!=''{hide} ORDER BY 1")]
        opts = {k: col(k) for k in ("model", "workflow", "category", "batch", "lora")}
    # 并入服务器已安装清单(model_meta):没生成过图的模型/LoRA 也出现在筛选下拉。
    # basename 归一去重;私密模式关闭时跳过标了私密的
    open_ = (db.get_setting("private_enabled") or "") == "1"
    for folder, key in (("checkpoints", "model"), ("loras", "lora")):
        cond = "" if open_ else " AND nsfw=0"
        have = {o.rsplit("/", 1)[-1] for o in opts[key]}
        for r in db.query(f"SELECT filename FROM model_meta WHERE folder=?{cond}", (folder,)):
            base = r["filename"].rsplit("/", 1)[-1]
            if base and base not in have:
                opts[key].append(base)
                have.add(base)
        opts[key].sort()
    return opts


def lookup_task_meta(filename):
    """按文件名在 comfyweb 库里找最近一条任务归属(images→tasks)。"""
    row = db.query_one(
        "SELECT t.model, t.category, t.batch, t.workflow_name, t.prompt_text, "
        "t.seed, t.lora, t.created_at, t.workflow_id FROM images i JOIN tasks t ON t.id=i.task_id "
        "WHERE i.filename=? AND i.type='output' ORDER BY i.id DESC LIMIT 1",
        (filename,))
    if not row:
        return None
    m = dict(row)
    m["workflow"] = m.pop("workflow_name", "") or ""
    m["prompt"] = m.pop("prompt_text", "") or ""
    # 任务的工作流被标私密 → 图归私密区(归档路由)
    m["nsfw"] = bool(db.query_one(
        "SELECT 1 FROM workflows WHERE id=? AND nsfw=1", (m.pop("workflow_id"),)))
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


def _is_tombstoned(filename):
    """已删除图片的墓碑:GPU 同步回流/本地缓冲补同步/收编/摄取都会重新带图,
    四条路径据此跳过,否则删除的图会悄悄复活。"""
    with _lock:
        row = _lib_connect().execute(
            "SELECT 1 FROM deleted_files WHERE filename=? LIMIT 1", (filename,)).fetchone()
    return bool(row)


def place(local_path: Path, filename, source="archive"):
    """把本地已有文件放入 library 树(分类 + 复制 + 缩略图延后 + 索引)。

    library 已有不低于本地质量的副本时跳过复制,只补索引。返回相对 nas_root 的路径;
    文件名在墓碑表里(用户已删除)时返回 None,调用方应清理本地副本。
    """
    if _is_tombstoned(filename):
        return None
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
               meta.get("prompt") if meta else "", meta.get("seed") if meta else None,
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
        if _is_tombstoned(p.name):
            # 用户已删除过的图又被 GPU 同步推回 output:直接清掉(NAS 回收站兜底)
            try:
                p.unlink()
            except OSError:
                pass
            skipped += 1
            continue
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
            log.warning("收编移动失败 %s: %s", p.name, e)
            continue
        rel = Path(library_dir().name) / dest.relative_to(library_dir())
        _index_add(rel, p.name, None,
                   meta.get("model") if meta else "", meta.get("category") if meta else "",
                   meta.get("batch") if meta else "", meta.get("workflow") if meta else "",
                   meta.get("prompt") if meta else "", meta.get("seed") if meta else None,
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
    """回滚:把已收编的图还原回 output/ 根并删除索引行(含缩略图)。
    运维工具(迁移回滚保底),无路由暴露;入参过 _safe_name 防路径穿越。"""
    row = indexed(filename)
    if not row:
        return False
    lib_file = nas_root() / row["path"]
    out = output_dir() / _safe_name(filename, 200)
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
        log.warning("library status 统计待收编失败", exc_info=True)
    return {"indexed": n, "unclassified": un, "ingested": ing, "no_thumb": nothumb,
            "last_snapshot": last, "pending": pending}
