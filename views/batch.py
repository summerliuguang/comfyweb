"""批量生成页与 API。生成引擎在 batchgen.py,AI 细化经懒加载复用 views.ai。"""
import json
import re

from flask import Blueprint, render_template, request

import batchgen
from comfy_client import ComfyError

from views.helpers import cached, err

bp = Blueprint("batch", __name__)


@bp.get("/batch")
def page_batch():
    return render_template("batch.html", active="batch")


def _ai():
    """懒加载 AI 模块:AI 被禁用/加载失败时只影响细化,批量生成其余部分不受影响。"""
    from views import ai
    return ai


def _chat_json(messages, max_tokens=4000, model=None):
    """LLM 调用 + 从回复中提取 JSON 对象(推理模型思维链已由 ai 模块剥离)。"""
    txt = _ai()._litegate_chat(messages, max_tokens=max_tokens, model=model)
    m = re.search(r"\{[\s\S]*\}", txt)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def _batch_default_model():
    """批量细化默认用免费模型(:free,如 nemotron);网关没有免费的再退回默认。"""
    ai = _ai()
    try:
        models = cached("ai:models", 60,
                        lambda: ai._fetch_ai_models(*ai._litegate_cfg()))
    except Exception:
        models = []
    free = sorted(m for m in models if ":free" in m)
    return free[0] if free else ai._litegate_default_model()


@bp.post("/api/batch/refine")
def api_batch_refine():
    if batchgen.STATE["running"]:
        return err("有批次正在运行,请等批次结束或先停止", 400)
    d = request.get_json(silent=True) or {}
    try:
        model = (d.get("model") or "").strip() or _batch_default_model()
        return {"tasks": batchgen.refine(d, _chat_json, model=model), "model": model}
    except ImportError:
        return err("AI 功能未启用或加载失败,无法细化任务清单", 503)
    except ComfyError as e:
        return err(e, 502)
    except ValueError as e:
        return err(e)


@bp.post("/api/batch/start")
def api_batch_start():
    d = request.get_json(silent=True) or {}
    try:
        total = batchgen.start(d.get("tasks"))
    except (ValueError, ComfyError) as e:
        return err(e)
    return {"ok": True, "total": total}


@bp.get("/api/batch/status")
def api_batch_status():
    import hoststats
    return {**batchgen.state(), "host": hoststats.fetch()}


@bp.post("/api/batch/stop")
def api_batch_stop():
    batchgen.stop()
    return {"ok": True}


@bp.get("/api/batch/templates")
def api_batch_templates():
    return {"templates": batchgen.templates_all()}


@bp.post("/api/batch/templates")
def api_batch_templates_post():
    d = request.get_json(silent=True) or {}
    action = d.get("action", "save")
    try:
        if action == "save":
            tpls = batchgen.template_save(d)
        elif action == "delete":
            tpls = batchgen.template_delete((d.get("name") or "").strip())
        else:
            return err("未知 action")
    except ValueError as e:
        return err(e)
    return {"ok": True, "templates": tpls}


def register(app):
    app.register_blueprint(bp)
