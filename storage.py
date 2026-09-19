"""图片归档存储:本地 data/images 为快速层与降级缓冲,canonical 正本入 NAS library
分类树(模型/任务结构 + 索引 + 缩略图,见 library.py)。

写路径:任务完成时后台线程从 ComfyUI 拉原图,总是先落本地(快、必成功),再异步
放进 library;library 写入慢(单文件超时)或失败进入冷却期,期间只累积本地,恢复后
自动补同步。读路径(/image 多级回退):本地缓冲 → library 索引/output 原位(NAS)
→ ComfyUI 实时代理。同步:library 已有不低于本地质量的副本 7 天后清理本地。
"""
import logging
import shutil
import threading
import time
from pathlib import Path

import db
import library

LOCAL_DIR = db.DATA_DIR / "images"     # 降级缓冲/快速层,永远存在(随 DATA_DIR,测试可隔离)
PRUNE_AFTER_DAYS = 7                   # 本地副本在 library 确认后的保留天数
SLOW_WRITE_SECONDS = 5                 # library 单文件写入超过此秒数视为"慢"
PENALTY_SECONDS = 300                  # 慢/失败后的冷却期,期间跳过 NAS 写入
SCAN_INTERVAL = 600                    # 全量补拉扫描间隔(秒)
COLLECT_INTERVAL = 300                 # output 收编扫描间隔(秒)
INGEST_INTERVAL = 900                  # 全库摄取(手工子目录)扫描间隔(秒)
SNAPSHOT_MIN_INTERVAL = 120            # 索引快照最小间隔(秒)
TICK_SECONDS = 15                      # 工作线程节拍

log = logging.getLogger("comfyweb")

_state_lock = threading.Lock()
_thread = None
_queue = []
_queued = set()          # 待处理去重
_penalty_until = 0.0     # NAS 冷却期截止
_index_dirty = False     # 索引有变更待快照
_wake = threading.Event()


# ---------- 可用性与冷却 ----------

def remote_enabled() -> bool:
    """library(含 output)是否处于本地盘之外的挂载点。"""
    return library.remote_enabled()


def remote_ready() -> bool:
    """NAS 当前是否可用(挂载在位且未处于慢写冷却期)。"""
    return remote_enabled() and time.time() >= _penalty_until


def _mark_penalty(reason):
    global _penalty_until
    _penalty_until = time.time() + PENALTY_SECONDS
    log.warning("图片存储 NAS 慢/不可用(%s),%d 秒内只写本地,恢复后自动补同步",
                reason, PENALTY_SECONDS)


def _rel_path(filename, subfolder, img_type):
    """本地缓冲相对路径 <type>/<subfolder>/<filename>;含 .. 或空文件名时拒绝。"""
    def safe(part):
        part = str(part or "").replace("\\", "/")
        segs = part.split("/")
        if any(seg == ".." for seg in segs):
            return None
        return [seg for seg in segs if seg not in ("", ".")]

    fname = safe(filename)
    if not fname:
        return None
    for pre in (safe(img_type), safe(subfolder)):
        if pre is None:
            return None
        fname = pre + fname
    return Path(*fname)


# ---------- 写路径 ----------

def enqueue(filename, subfolder, img_type):
    """任务完成后请求归档一张原图(后台线程去重执行)。"""
    rel = _rel_path(filename, subfolder, img_type)
    if rel is None:
        return
    key = str(rel)
    with _state_lock:
        if key in _queued:
            return
        _queued.add(key)
        _queue.append((filename, subfolder, img_type))
    _wake.set()


def _download(filename, subfolder, img_type) -> bytes | None:
    from comfy_client import ComfyError, client
    try:
        r = client.download_image(filename, subfolder, img_type)
    except ComfyError as e:
        log.warning("归档取图失败 %s: %s", filename, e)
        return None
    body = r.content
    r.close()
    return body


def _thumb_cache_fallback(filename, subfolder, img_type) -> bytes | None:
    """原图在 ComfyUI 侧已缺失时,退而求其次用本机缩略图缓存做归档副本。"""
    from views.helpers import img_cache_get
    for preview in ("webp;jpeg;70", "webp;jpeg;75"):
        body_path, _ct = img_cache_get(f"img:{img_type}:{subfolder}:{filename}:{preview}")
        if body_path:
            try:
                return body_path.read_bytes()
            except OSError:
                continue
    return None


def _archive_one(filename, subfolder, img_type):
    """拉原图 → 本地缓冲(必成功) → 放入 library(分类+缩略图+索引)。"""
    rel = _rel_path(filename, subfolder, img_type)
    if rel is None:
        return
    local_path = LOCAL_DIR / rel
    if not local_path.exists():
        body = _download(filename, subfolder, img_type)
        if body is None:
            body = _thumb_cache_fallback(filename, subfolder, img_type)
            if body is None:
                return  # 归档无源(原图与缩略图缓存都没有):读路径走实时代理兜底
            log.info("原图已缺失,用缩略图缓存归档 %s", filename)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = local_path.with_suffix(local_path.suffix + ".part")
        tmp.write_bytes(body)
        tmp.replace(local_path)  # 先写临时文件再原子改名,避免读到半个文件
    if not remote_ready():
        return  # 本地已保底;冷却期过后 sync_pending 会补放置
    try:
        global _index_dirty
        t0 = time.time()
        library.place(local_path, filename, source="archive")
        _index_dirty = True
        if time.time() - t0 > SLOW_WRITE_SECONDS:
            _mark_penalty(f"入库 {filename} 耗时 {time.time() - t0:.0f}s")
    except OSError as e:
        _mark_penalty(str(e)[:80])


# ---------- 读路径 ----------

def find_archived(filename, subfolder, img_type):
    """读路径前几级:本地缓冲 → library 索引(NAS) → output 原位(NAS)。

    返回文件路径或 None(最后由 /image 回源 ComfyUI 实时代理)。
    """
    rel = _rel_path(filename, subfolder, img_type)
    if rel is not None:
        p = LOCAL_DIR / rel
        if p.exists():
            return p
    if remote_ready():
        try:
            return library.find_on_nas(filename, subfolder, img_type)
        except OSError:
            pass
    return None


# ---------- 补拉与同步 ----------

def _enqueue_missing():
    """把库里还没有任何归档落点(本地/library/output 都没有)的图片排进补拉队列。"""
    rows = db.query("SELECT filename, subfolder, type FROM images WHERE type='output'")
    missing = 0
    for r in rows:
        rel = _rel_path(r["filename"], r["subfolder"], r["type"])
        if rel is not None and (LOCAL_DIR / rel).exists():
            continue
        if remote_enabled():
            try:
                if library.find_on_nas(r["filename"], r["subfolder"], r["type"]):
                    continue
            except OSError:
                pass
        enqueue(r["filename"], r["subfolder"], r["type"])
        missing += 1
    if missing:
        log.info("图片补拉:发现 %d 张缺归档,开始后台拉取", missing)


def sync_pending():
    """把本地有、library 没有的副本补放进去;确认 library 已有不低于本地质量
    副本的,超龄本地文件释放(含画质升级:library 全画质原图 > 本地 webp 替身)。"""
    if not remote_ready():
        return
    now = time.time()
    placed = pruned = 0
    for p in LOCAL_DIR.rglob("*"):
        if not p.is_file() or p.suffix == ".part":
            continue
        filename = p.name
        try:
            row = library.indexed(filename)
            if row:
                dest = library.nas_root() / row["path"]
                if dest.exists() and dest.stat().st_size >= p.stat().st_size:
                    # library 副本不低于本地:超龄释放(读路径自动落到 library)
                    if now - p.stat().st_mtime > PRUNE_AFTER_DAYS * 86400:
                        p.unlink()
                        pruned += 1
                    continue
            if not remote_ready():
                return  # 冷却期:保留本地,下次再试
            t0 = time.time()
            library.place(p, filename, source="sync")
            global _index_dirty
            _index_dirty = True
            if time.time() - t0 > SLOW_WRITE_SECONDS:
                _mark_penalty(f"入库 {filename} 耗时 {time.time() - t0:.0f}s")
                return
            placed += 1
        except OSError as e:
            _mark_penalty(str(e)[:80])
            return
    if placed or pruned:
        log.info("图片同步:入库 %d 张,清理本地旧副本 %d 张", placed, pruned)


def trigger_sync():
    """立即做一轮补拉扫描 + 同步(设置页按钮/启动时)。"""
    _wake.set()


# ---------- 后台线程 ----------

def _loop():
    last_scan = 0.0
    last_collect = 0.0
    last_ingest = 0.0
    last_snapshot = 0.0
    while True:
        global _index_dirty
        try:
            while True:
                with _state_lock:
                    item = _queue.pop(0) if _queue else None
                if item is None:
                    break
                try:
                    _archive_one(*item)
                except Exception:
                    log.exception("归档单张失败 %s", item[0])
                finally:
                    with _state_lock:
                        _queued.discard(str(_rel_path(*item)))
                time.sleep(0.3)  # 轻微限速,不挤占 ComfyUI
            if time.time() - last_scan > SCAN_INTERVAL:
                _enqueue_missing()
                last_scan = time.time()
            if remote_ready() and time.time() - last_collect > COLLECT_INTERVAL:
                moved, _skipped = library.collect_once(limit=50)
                if moved:
                    log.info("收编 output 图片 %d 张入库", moved)
                    _index_dirty = True
                last_collect = time.time()
            if remote_ready() and time.time() - last_ingest > INGEST_INTERVAL:
                changed = library.ingest_refresh()
                if changed:
                    log.info("全库摄取:新增/更新 %d 张索引", changed)
                    _index_dirty = True
                last_ingest = time.time()
            if remote_ready():
                done = library.thumb_sweep(limit=6)  # 逐批补齐缺缩略图的行
                if done:
                    _index_dirty = True
            sync_pending()
            if _index_dirty and remote_ready() and time.time() - last_snapshot > SNAPSHOT_MIN_INTERVAL:
                try:
                    library.snapshot()
                    _index_dirty = False
                    last_snapshot = time.time()
                except OSError as e:
                    log.warning("索引快照推送失败: %s", e)
        except Exception:
            log.exception("图片存储工作线程异常")
        _wake.wait(TICK_SECONDS)
        _wake.clear()


def start():
    """启动归档工作线程(幂等);启动即触发一轮补拉扫描。"""
    global _thread
    with _state_lock:
        if _thread and _thread.is_alive():
            return
        _thread = threading.Thread(target=_loop, name="imgstore", daemon=True)
        _thread.start()
    log.info("图片归档线程已启动(library: %s)", library.library_dir())


def status():
    """设置页展示:library 配置/挂载/降级状态 + 本地缓冲与收编概况。"""
    local_n = sum(1 for p in LOCAL_DIR.rglob("*") if p.is_file() and p.suffix != ".part")
    pending = 0
    if remote_enabled():
        for p in LOCAL_DIR.rglob("*"):
            if not p.is_file() or p.suffix == ".part":
                continue
            row = library.indexed(p.name)
            try:
                dest = (library.nas_root() / row["path"]) if row else None
                if dest is None or not dest.exists() or dest.stat().st_size < p.stat().st_size:
                    pending += 1
            except OSError:
                pending += 1
    st = library.status()
    st["pending_collect"] = st.pop("pending", 0)  # library 待收编与本地待入库分开计数
    st.update({
        "dir": str(library.library_dir()),
        "remote": remote_enabled(),
        "degraded": remote_enabled() and time.time() < _penalty_until,
        "local_files": local_n,
        "pending": pending,
    })
    return st
