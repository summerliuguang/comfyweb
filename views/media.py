"""媒体基础设施:生成图片代理。

画廊、收藏、生成任务卡的缩略图与原图全部经此回源 ComfyUI,属共享基础设施,
不参与功能开关——关闭"生成与工作流"不影响画廊图片加载。
"""
import os
from urllib.parse import quote as urlquote
from uuid import uuid4

from flask import Blueprint, Response, request

from comfy_client import ComfyError, client

from views.helpers import err, img_cache_get, img_cache_store

bp = Blueprint("media", __name__)


@bp.get("/image")
def image_proxy():
    filename = request.args.get("filename", "")
    if not filename:
        return err("缺少 filename")
    subfolder = request.args.get("subfolder", "")
    img_type = request.args.get("type", "output")
    preview = request.args.get("preview")
    # 仅缩略图落盘缓存(原图体积大,仍实时回源)
    ck = None
    if preview:
        ck = f"img:{img_type}:{subfolder}:{filename}:{preview}"
        cached_path, cached_ct = img_cache_get(ck)
        if cached_path:
            return Response(cached_path.read_bytes(),
                            content_type=cached_ct or "image/jpeg",
                            headers={"Cache-Control": "public, max-age=604800"})
    try:
        r = client.download_image(filename, subfolder, img_type, preview)
    except ComfyError as e:
        return err(e, 502)
    body = r.content
    r.close()
    if ck:
        img_cache_store(ck, body, r.headers.get("Content-Type", "image/jpeg"))
    headers = {"Cache-Control": "public, max-age=604800"}
    if request.args.get("dl"):
        # 中文前缀的文件名不能直接进 HTTP 头,按 RFC 5987 提供 UTF-8 文件名
        ext = os.path.splitext(filename)[1] or ".png"
        ascii_name = f"comfyweb_{uuid4().hex[:8]}{ext}"
        utf8_name = urlquote(filename)
        headers["Content-Disposition"] = (
            f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{utf8_name}')
    return Response(body,
                    content_type=r.headers.get("Content-Type", "image/png"),
                    headers=headers)


def register(app):
    app.register_blueprint(bp)
