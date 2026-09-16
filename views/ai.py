"""AI 提示词助手:LiteGate 网关调用(润色/翻译/模型列表)与 Danbooru tag 联想。

batch 模块经懒加载复用本模块的网关调用;本模块加载失败时批量生成其余功能不受影响。
"""
import os
import re
import threading

import requests

from flask import Blueprint, request

from comfy_client import ComfyError

from views.helpers import cached, err

bp = Blueprint("ai", __name__)

AI_POLISH_STYLES = {
    "enhance": "在保持原意的前提下增强画面表现力:补全主体特征、环境氛围、光影与构图关键词。",
    "detail": "侧重细节堆叠:材质、纹理、服饰细节、背景元素,输出更丰富的画面描述。",
    "anime": "面向动漫风格出图:使用 Danbooru 社区常用 tag 风格(如 1girl, solo),"
             "补充画质词(best quality, masterpiece)与角色特征。",
    "photo": "面向写实摄影风格:补充相机、镜头、光线、景深、胶片质感等摄影关键词。",
    "concise": "精简为最有效的高权重 tag,去掉冗余修饰,保留主体与关键画风。",
}


def _litegate_cfg():
    base = os.environ.get("LITEGATE_BASE", "http://127.0.0.1:8080/v1").rstrip("/")
    key = os.environ.get("LITEGATE_API_KEY", "")
    return base, key


def _litegate_default_model():
    return os.environ.get("LITEGATE_MODEL", "deepseek-flash")


def _litegate_chat(messages, max_tokens=2000, model=None):
    base, key = _litegate_cfg()
    if not key:
        raise ComfyError("未配置 LITEGATE_API_KEY(.env)")
    insecure = os.environ.get("LITEGATE_INSECURE") == "1"  # 自签证书的内网网关
    if insecure:
        requests.packages.urllib3.disable_warnings()
    # 大清单输出(批量细化 4000 tokens)在推理模型上要跑几分钟,按规模放宽超时
    timeout = max(120, min(300, max_tokens // 10))
    try:
        r = requests.post(
            f"{base}/chat/completions", timeout=timeout, verify=not insecure,
            headers={"Authorization": f"Bearer {key}",
                     "X-LiteGate-App": "comfyweb",
                     "Content-Type": "application/json"},
            json={"model": model or _litegate_default_model(),
                  "max_tokens": max_tokens, "messages": messages})
    except requests.RequestException as e:
        raise ComfyError(f"AI 网关连接失败: {e}")
    if r.status_code != 200:
        raise ComfyError(f"AI 网关错误 {r.status_code}: {r.text[:200]}")
    try:
        txt = (r.json().get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
    except ValueError:
        raise ComfyError("AI 网关返回非 JSON")
    if not txt:
        # deepseek-flash 是推理模型:思维链可能吃掉 max_tokens 导致正文为空
        raise ComfyError("AI 返回为空(思维链耗尽 token?)")
    # 推理模型的思维链不属于正文,润色/翻译/细化都不该带出去
    return re.sub(r"<think>[\s\S]*?</think>", "", txt).strip()


# 对话用不上的模型(语音/向量/重排等)不在润色/翻译下拉中展示
_AI_NON_CHAT = re.compile(r"asr|tts|embed|rerank|whisper|moderation|guard", re.I)


@bp.get("/api/ai/models")
def api_ai_models():
    base, key = _litegate_cfg()
    if not key:
        return {"models": [], "default": _litegate_default_model()}
    models = cached("ai:models", 60, lambda: _fetch_ai_models(base, key))
    return {"models": models, "default": _litegate_default_model()}


def _fetch_ai_models(base, key):
    try:
        r = requests.get(f"{base}/models", timeout=10,
                         headers={"Authorization": f"Bearer {key}"})
        ids = [m.get("id", "") for m in r.json().get("data", [])] if r.ok else []
    except (requests.RequestException, ValueError):
        return []
    return sorted(i for i in ids if i and not _AI_NON_CHAT.search(i))


@bp.post("/api/ai/polish")
def api_ai_polish():
    d = request.get_json(silent=True) or {}
    text = (d.get("text") or "").strip()
    if not text:
        return err("内容为空")
    guide = AI_POLISH_STYLES.get(d.get("style") or "", AI_POLISH_STYLES["enhance"])
    sys_p = ("你是 Stable Diffusion 提示词专家。把用户的提示词重写为英文,要求:" + guide +
             "\n只输出提示词本身,逗号分隔小写 tag 风格,不要解释、不要引号、不要 markdown。")
    try:
        txt = _litegate_chat([{"role": "system", "content": sys_p},
                              {"role": "user", "content": text}],
                             model=(d.get("model") or "").strip() or None)
    except ComfyError as e:
        return err(e, 502)
    return {"text": txt}


@bp.post("/api/ai/translate")
def api_ai_translate():
    d = request.get_json(silent=True) or {}
    text = (d.get("text") or "").strip()
    if not text:
        return err("内容为空")
    has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in text)
    if has_cjk:
        sys_p = ("把用户的中文描述翻译成 Stable Diffusion 提示词风格的英文 tag 序列,"
                 "人名等专有名词保留,只输出英文提示词本身,不要解释。")
    else:
        sys_p = "把用户的英文提示词翻译成通顺的中文描述,只输出中文,不要解释。"
    try:
        txt = _litegate_chat([{"role": "system", "content": sys_p},
                              {"role": "user", "content": text}],
                             model=(d.get("model") or "").strip() or None)
    except ComfyError as e:
        return err(e, 502)
    return {"text": txt}


# ---------- Danbooru tag 联想 ----------

_TAG_CATS = {0: "通用", 1: "画师", 2: "作品", 3: "角色", 4: "元数据", 5: "元数据"}
_tags_lock = threading.Lock()
_tags_list = []          # (name, cat, count),按热度降序
_tags_loaded = False


def _load_tags():
    """懒加载 data/tags/danbooru.csv(name,category,count[,aliases]),启动后首次联想时读一次。"""
    global _tags_list, _tags_loaded
    with _tags_lock:
        if _tags_loaded:
            return True
        import csv
        import os as _os
        f = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                          "data", "tags", "danbooru.csv")
        if not _os.path.exists(f):
            return False
        rows = []
        with open(f, encoding="utf-8", newline="") as fh:
            for row in csv.reader(fh):
                if len(row) < 3:
                    continue
                try:
                    rows.append((row[0].strip().lower(), int(row[1]), int(row[2])))
                except ValueError:
                    continue
        rows.sort(key=lambda r: -r[2])
        _tags_list = rows
        _tags_loaded = True
        return True


@bp.get("/api/tags/search")
def api_tags_search():
    q = request.args.get("q", "").strip().lower()
    if not q or not _load_tags():
        return {"tags": []}
    # 前缀命中优先,不足再补子串命中;tag 列表本身已按热度降序
    hits = [r for r in _tags_list if r[0].startswith(q)]
    if len(hits) < 12:
        hits += [r for r in _tags_list if q in r[0] and not r[0].startswith(q)]
    hits = hits[:12]
    return {"tags": [{"name": r[0], "cat": _TAG_CATS.get(r[1], ""), "count": r[2]}
                     for r in hits]}


def register(app):
    app.register_blueprint(bp)
    # tag 联想列表 14 万行,启动时后台加载,首次输入即命中
    threading.Thread(target=_load_tags, daemon=True).start()
