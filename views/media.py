"""媒体基础设施:生成图片代理。

画廊、收藏、生成任务卡的缩略图与原图全部经此回源 ComfyUI,属共享基础设施,
不参与功能开关——关闭"生成与工作流"不影响画廊图片加载。
读路径:已归档的原图(本地 data/images → 可配置存储目录)优先,未归档的回源 ComfyUI。
"""
import os
import threading
from pathlib import Path
from urllib.parse import quote as urlquote
from uuid import uuid4

from flask import Blueprint, Response, redirect, request

from comfy_client import ComfyError, client

from views.helpers import err, img_cache_get, img_cache_store

bp = Blueprint("media", __name__)

# 现场生成缩略图的并发上限:首屏 24 张全缺时防止 8 个 gthread 全部阻塞在 CIFS 读
_thumb_gen_sema = threading.Semaphore(2)

_CTYPE = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
          ".webp": "image/webp", ".gif": "image/gif"}


def _sniff_ctype(head: bytes, fallback: str) -> str:
    """按魔数识别图片真实格式(归档副本可能是缩略图缓存,扩展名不可信)。"""
    if head.startswith(b"\x89PNG"):
        return "image/png"
    if head.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return fallback


def _download_headers(filename):
    headers = {"Cache-Control": "public, max-age=604800"}
    if not request.args.get("dl"):
        return headers
    # 中文前缀的文件名不能直接进 HTTP 头,按 RFC 5987 提供 UTF-8 文件名
    ext = os.path.splitext(filename)[1] or ".png"
    ascii_name = f"comfyweb_{uuid4().hex[:8]}{ext}"
    utf8_name = urlquote(filename)
    headers["Content-Disposition"] = (
        f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{utf8_name}')
    return headers


def _archived_fallback(filename, subfolder, img_type):
    """归档副本兜底:返回 (bytes, content_type) 或 None(无归档/不可读)。

    原图在 ComfyUI 侧被清理后,归档里的替身(可能是缩略图缓存副本)是唯一残存来源。
    """
    try:
        import storage
        p = storage.find_archived(filename, subfolder, img_type)
    except Exception:
        return None
    if p is None:
        return None
    try:
        body = p.read_bytes()
    except OSError:
        return None
    return body, _sniff_ctype(body[:16], _CTYPE.get(p.suffix.lower(), "image/png"))


@bp.get("/image")
def image_proxy():
    filename = request.args.get("filename", "")
    if not filename:
        return err("缺少 filename")
    subfolder = request.args.get("subfolder", "")
    img_type = request.args.get("type", "output")
    preview = request.args.get("preview")
    ck = None

    # 缩略图:本地磁盘缓存(内容不可变)
    if preview:
        ck = f"img:{img_type}:{subfolder}:{filename}:{preview}"
        cached_path, cached_ct = img_cache_get(ck)
        if cached_path:
            try:
                return Response(cached_path.read_bytes(),
                                content_type=cached_ct or "image/jpeg",
                                headers={"Cache-Control": "public, max-age=604800"})
            except OSError:
                pass  # 缓存被并发淘汰:视为未命中,走回源(四级回退自愈)
    else:
        # 原图:已归档的优先读归档(ComfyUI 侧清理输出后仍可看)
        fb = _archived_fallback(filename, subfolder, img_type)
        if fb is not None:
            return Response(fb[0], content_type=fb[1],
                            headers=_download_headers(filename))

    try:
        r = client.download_image(filename, subfolder, img_type, preview)
    except ComfyError as e:
        if preview:
            # 缩略图生成失败(原图多半已被 ComfyUI 清理):回退归档副本,
            # 并回填缩略图缓存自愈——缓存条目被淘汰后也能从归档恢复
            fb = _archived_fallback(filename, subfolder, img_type)
            if fb is not None:
                if len(fb[0]) <= 512 * 1024:  # 替身是缩略图尺寸才回填,原图太大不进缩略图缓存
                    img_cache_store(ck, fb[0], fb[1])
                return Response(fb[0], content_type=fb[1],
                                headers={"Cache-Control": "public, max-age=604800"})
        return err(e, 502)
    body = r.content
    r.close()
    if preview:
        img_cache_store(ck, body, r.headers.get("Content-Type", "image/jpeg"))
    return Response(body,
                    content_type=r.headers.get("Content-Type", "image/png"),
                    headers=_download_headers(filename))


def register(app):
    app.register_blueprint(bp)


# ---------- 整理库媒体(画廊全量浏览;rowid 寻址,无路径输入面) ----------

@bp.get("/libthumb/<int:rowid>")
def lib_thumb(rowid):
    """整理库缩略图:本地缓存 → NAS thumbs → 现场生成(Pillow)并回填。"""
    import library
    row = library.get(rowid)
    if not row:
        return err("图片不存在", 404)
    ck = "libthumb:" + row["path"]
    cached_path, cached_ct = img_cache_get(ck)
    if cached_path:
        try:
            return Response(cached_path.read_bytes(), content_type=cached_ct or "image/webp",
                            headers={"Cache-Control": "public, max-age=604800"})
        except OSError:
            pass  # 缓存被并发淘汰:视为未命中
    body = None
    if row["thumb"]:
        try:
            body = (library.nas_root() / row["thumb"]).read_bytes()
        except OSError:
            body = None
    if body is None:
        # 现场生成(读 CIFS 原图 + Pillow)有并发上限;超限时先用原图顶替,
        # 后台 thumb_sweep 会继续补齐缩略图
        if not _thumb_gen_sema.acquire(blocking=False):
            return redirect(f"/libmedia/{rowid}", 302)
        try:
            body = library.thumb_generate(row)  # 现场生成并回填索引/NAS
        finally:
            _thumb_gen_sema.release()
        if body is None:
            return err("缩略图不可用", 404)
    img_cache_store(ck, body, "image/webp")
    return Response(body, content_type="image/webp",
                    headers={"Cache-Control": "public, max-age=604800"})


@bp.get("/libmedia/<int:rowid>")
def lib_media(rowid):
    """整理库原图(查看/下载)。"""
    import library
    row = library.get(rowid)
    if not row:
        return err("图片不存在", 404)
    try:
        body = (library.nas_root() / row["path"]).read_bytes()
    except OSError:
        return err("图片文件不可读(NAS 未挂载或已移动)", 502)
    ctype = _sniff_ctype(body[:16], _CTYPE.get(Path(row["path"]).suffix.lower(), "image/png"))
    return Response(body, content_type=ctype, headers=_download_headers(row["filename"]))
