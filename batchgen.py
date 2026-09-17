"""批量生成:AI 细化提示词生成任务清单 + 按模型分组依次生成的后台引擎。

流程:用户输入大概要求 -> LiteGate LLM 按内置的模型风格参考产出任务清单
(人物/图标走 anima_turbo 英文标签风,纯背景走 z-image-turbo 中文自然句风)
-> 用户可增删改 -> 引擎按管线分组依次提交 ComfyUI,组间卸载显存,每张前
可选检查 GPU 温度;每张图落一条 tasks 记录,进度经 WS 常驻线程自动跟踪,
图片自动进入画廊(工作流名「批量生成」,可按模型筛选)。
"""
import json
import os
import re
import secrets
import threading
import time

import db
from comfy_client import ComfyError, client
import hoststats

MAX_BATCH = 60
BATCH_WF_NAME = "批量生成"

# 每张图完成后等 history 的超时:z-image 1216x832 实测 90s 内,留足余量
TASK_TIMEOUT = 420


# ---------- 内置管线预设(与 ComfyUI 主机已装模型对应;尺寸:竖版人物/横版场景/方版图标) ----------

NEG_BASE = ("lowres, bad anatomy, bad hands, text, watermark, jpeg artifacts, worst quality")

PIPELINES = {
    "anima": {
        "label": "anima_turbo(人物/图标/特效)",
        "model": "anima_turboV11.safetensors",
        "steps": 8,
        "unet_dir": "diffusion_models",
        "clip": {"clip_name": "qwen_3_06b_base.safetensors", "type": "stable_diffusion"},
        "vae": "qwen_image_vae.safetensors",
    },
    "zimage": {
        "label": "z-image-turbo(纯背景/氛围)",
        "model": "z-image-turbo-fp8-e4m3fn.safetensors",
        "steps": 10,
        "unet_dir": "diffusion_models",
        "clip": {"clip_name": "qwen_3_4b.safetensors", "type": "qwen_image"},
        "vae": "ae.safetensors",
    },
}

# 构图锚点:desc 之外由服务端按任务类型追加,保证版式统一
SPR_ANCHOR = ("standing, full body, white background, simple background, "
              "visual novel character sprite, soft lighting, detailed, masterpiece, best quality")
SCENE_ANCHOR = "in scene, detailed environment background, cinematic lighting, masterpiece, best quality"
ICON_ANCHOR = "game item illustration, single object, floating, transparent simple background, glowing, detailed, masterpiece"

# 任务类型 -> (管线, 宽, 高, 画廊筛选用的类型标签)
TYPE_LAYOUT = {
    "character": ("anima", 832, 1216, "立绘"),
    "icon": ("anima", 832, 832, "图标"),
    "scene": ("anima", 1216, 832, "场景"),
    "bg": ("zimage", 1216, 832, "背景"),
}


def build_graph(pipe_key, pos, neg, seed, w, h):
    """文生图 API 工作流(UNETLoader + CLIPLoader + VAELoader,KSampler CFG1)。"""
    p = PIPELINES[pipe_key]
    return {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": p["model"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {**p["clip"], "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": p["vae"]}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": pos, "clip": ["2", 0]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": neg, "clip": ["2", 0]}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": int(w), "height": int(h), "batch_size": 1}},
        "3.5": {"class_type": "KSampler",
                "inputs": {"seed": int(seed), "steps": p["steps"], "cfg": 1, "sampler_name": "euler",
                           "scheduler": "simple", "denoise": 1, "model": ["1", 0],
                           "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3.5", 0], "vae": ["3", 0]}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": "batchgen"}},
    }


# ---------- AI 细化(LLM 任务清单) ----------

MODEL_STYLES = {
    "anima": """【anima_turbo 模型——人物立绘/图标/特效(英文短标签风)】
- 逗号分隔的英文短语标签(danbooru 风格词汇),顺序:数量主体(1girl/1boy, solo) → 外貌细节(发型/瞳色/脸型) → 服装 → 动作表情 → 构图与背景锚点 → 质量词
- 服务端会自动追加构图锚点,desc 专注人物/物品本身
- 纯英文,严禁中文;外貌特征要明确(发色发型瞳色身材)
- 示例 desc:"gentle japanese schoolgirl, long straight black hair, hime-cut bangs, navy sailor uniform, holding sketchbook, soft smile"
- 图标类示例 desc:"glowing spirit stone, translucent jade crystal, cyan inner light, light particles\"""",
    "zimage": """【z-image-turbo 模型——纯背景/氛围图(中文自然句)】
- 中文完整句子,顺序:场景主体 → 建筑/自然细节 → 光线与时间 → 氛围词 → 质量收尾("视觉小说场景背景图,精致细腻,杰作")
- 该模型能正确渲染汉字:需要文字时直接给全文("牌匾上题着「七玄门」三个金色大字,文字清晰正确")
- 无人场景必须写"无人物";需要人物时用剪影表达("人物剪影")
- 示例 desc:"古代修仙坊市街道,木质店铺摊位,悬挂红灯笼,清晨薄雾,暖色阳光,无人物,视觉小说场景背景图,精致细腻,杰作\"""",
}

REFINE_SYS = """你是游戏美术资深的提示词设计师。根据用户需求设计一批图片任务,输出严格 JSON(不要解释)。
格式:
角色套图模式: {"characters":[{"cn":"角色中文名","look":"english appearance","outfit":"english outfit","scenes":[{"cn":"场景中文名","desc":"english scene description with the character"}]}]}
自由画面模式: {"shots":[{"cn":"画面中文名","type":"character|scene|bg|icon","desc":"english description"}]}
规则:
1. 描述风格必须遵循下方注入的《模型提示词风格参考》,desc 会与用户固定提示词模板拼装使用。
2. 人物描述含外貌与气质;场景描述含人物动作与环境。
3. 严禁 text/watermark 相关内容;保持全项目风格统一。
4. 数量严格按用户要求。

《模型提示词风格参考》
"""


def _refine_user_prompt(mode, theme, count, style):
    if mode == "cast":
        return (f"设计 {count} 位角色,每人 1 张立绘 + 6 个剧情场景"
                f"(战斗/日常/危机/觉醒/黑暗面/羁绊 之类贴合主题的弧线)。主题:{theme}。"
                f"风格补充:{style or '无'}")
    return (f"设计 {count} 张画面。主题:{theme}。风格补充:{style or '无'}。"
            f"type 从 character/scene/bg/icon 中按内容选择。")


def refine(body, llm_chat, model=None):
    """调 LLM 生成任务清单。llm_chat(messages, max_tokens, model) 由 app 注入。

    返回任务数组 [{name, pipeline, w, h, prompt, neg, seed}];三次重试防空清单。
    """
    mode = body.get("mode", "free")
    if mode not in ("free", "cast"):
        raise ValueError("无效的模式")
    theme = (body.get("theme") or "").strip()
    if not theme:
        raise ValueError("主题为空")
    try:
        # 套图每人展开 1 立绘 + 6 场景 = 7 张,8 人封顶(56 张)才不超 start 的 MAX_BATCH
        count = max(1, min(int(body.get("count") or 6), 8 if mode == "cast" else MAX_BATCH))
    except (TypeError, ValueError):
        count = 6
    style = (body.get("style") or "").strip()
    tpl = body.get("template") or {}
    tpl_pos = (tpl.get("positive") or "").strip()
    tpl_neg = (tpl.get("negative") or "").strip()

    sys_p = REFINE_SYS + "\n\n".join(MODEL_STYLES.values()) + f"\n\n本次模式为 {mode},按上述风格参考设计 desc。"
    tasks = []
    for _attempt in range(3):  # LLM 偶发返回空清单,重试最多 3 次
        data = llm_chat([{"role": "system", "content": sys_p},
                         {"role": "user", "content": _refine_user_prompt(mode, theme, count, style)}],
                        max_tokens=4000, model=model)
        if not data:
            continue
        seed = secrets.randbits(31)
        if mode == "cast" and data.get("characters"):
            for ch in data["characters"][:count]:
                base = f"{ch.get('look', '')}, {ch.get('outfit') or 'elegant outfit'}"
                tasks.append({"name": f"{ch.get('cn', '角色')} · 立绘", "pipeline": "anima",
                              "w": 832, "h": 1216, "category": "立绘",
                              "prompt": f"{base}, {SPR_ANCHOR}", "neg": NEG_BASE})
                for sc in (ch.get("scenes") or [])[:6]:
                    tasks.append({"name": f"{ch.get('cn', '角色')} · {sc.get('cn', '场景')}",
                                  "pipeline": "anima", "w": 1216, "h": 832, "category": "场景",
                                  "prompt": f"{base}, {sc.get('desc', '')}, {SCENE_ANCHOR}", "neg": NEG_BASE})
            break
        if mode == "free" and data.get("shots"):
            for shot in data["shots"][:count]:
                t = shot.get("type", "scene")
                if t not in TYPE_LAYOUT:
                    t = "scene"
                pipe, w, h, cat = TYPE_LAYOUT[t]
                desc = shot.get("desc", "")
                if pipe == "zimage":
                    pos = desc
                elif t == "icon":
                    pos = f"{desc}, {ICON_ANCHOR}"
                elif t == "character":
                    pos = f"{desc}, {SPR_ANCHOR}"
                else:
                    pos = f"{desc}, {SCENE_ANCHOR}"
                tasks.append({"name": shot.get("cn", "画面"), "pipeline": pipe, "category": cat,
                              "w": w, "h": h, "pos_prompt": desc, "prompt": pos, "neg": NEG_BASE})
            break
    if not tasks:
        raise ValueError("AI 三次都未返回有效任务清单,请重试或换个说法")
    for i, t in enumerate(tasks):
        t["seed"] = (seed + i) % (2 ** 31)
        t["batch"] = theme[:60]  # 批次主题,画廊筛选维度
        # 用户固定提示词模板:positive 作前缀,negative 叠加到基础负面词
        if tpl_pos:
            t["prompt"] = f"{tpl_pos}, {t['prompt']}"
        if tpl_neg:
            t["neg"] = f"{t['neg']}, {tpl_neg}"
    return tasks


# ---------- 提示词模板 CRUD ----------

def templates_all():
    return [dict(r) for r in db.query(
        "SELECT name, positive, negative FROM batch_templates ORDER BY updated_at DESC")]


def template_save(d):
    name = (d.get("name") or "").strip()
    if not name:
        raise ValueError("模板名为空")
    if len(name) > 50:
        raise ValueError("模板名过长")
    db.execute(
        "INSERT INTO batch_templates(name, positive, negative, updated_at) VALUES(?,?,?,datetime('now','localtime')) "
        "ON CONFLICT(name) DO UPDATE SET positive=excluded.positive, negative=excluded.negative, "
        "updated_at=datetime('now','localtime')",
        (name, (d.get("positive") or "").strip()[:2000], (d.get("negative") or "").strip()[:1000]))
    return templates_all()


def template_delete(name):
    db.execute("DELETE FROM batch_templates WHERE name=?", (name,))
    return templates_all()


# ---------- 生成引擎 ----------

STATE = {"running": False, "stop": False, "done": 0, "total": 0, "current": "",
         "temp": "", "log": [], "finished": False, "err": 0}
_state_lock = threading.Lock()


def state():
    with _state_lock:
        return dict(STATE, log=list(STATE["log"]))


def log(msg):
    with _state_lock:
        STATE["log"].append(f"{time.strftime('%H:%M:%S')} {msg}")
        STATE["log"] = STATE["log"][-80:]


def gpu_temp_enabled():
    """温度保护默认关:仅当能取到 GPU 温度(SSH 跨机或本机)时有意义(.env 开启)。"""
    return os.environ.get("BATCHGEN_GPU_TEMP") == "1"


def gpu_temp():
    """ComfyUI 主机的 GPU 温度:跨机走 SSH,同机走本机 nvidia-smi;取不到返回 0。"""
    return hoststats.gpu_temp()


def _update_temp_display():
    st = hoststats.fetch()
    if st.get("temp"):
        txt = f"GPU {st['temp']}°C"
        if st.get("util"):
            txt += f" · 利用率 {st['util']}%"
        with _state_lock:
            STATE["temp"] = txt
    return st.get("temp", 0) or 0


def free_vram():
    """管线切换/批次结束卸载模型(ComfyUI 切大模型显存不足会崩)。"""
    try:
        client.post_json("/free", {"unload_models": True, "free_memory": True})
        time.sleep(2)
    except ComfyError:
        pass


def _insert_task(prompt_id, t):
    cur = db.execute(
        "INSERT INTO tasks(prompt_id, workflow_name, prompt_text, seed, model, batch, category, "
        "params_json, status, count) VALUES(?,?,?,?,?,?,?,?, 'queued',1)",
        (prompt_id, BATCH_WF_NAME, t.get("pos_prompt") or t["prompt"], t["seed"],
         PIPELINES[t["pipeline"]]["model"], t.get("batch", ""), t.get("category", ""),
         json.dumps([{"label": "尺寸", "value": f"{t['w']}x{t['h']}"},
                     {"label": "提示词", "value": t["prompt"]}], ensure_ascii=False)))
    return cur.lastrowid


def _wait_task(prompt_id):
    """等 WS 线程把任务落库为 done;停滞超时用 history 对账兜底。"""
    deadline = time.time() + TASK_TIMEOUT
    while time.time() < deadline:
        if STATE["stop"]:
            return "stopped"
        time.sleep(2)
        row = db.query_one("SELECT status FROM tasks WHERE prompt_id=?", (prompt_id,))
        if row and row["status"] in ("done", "error", "canceled"):
            return row["status"]
        if time.time() - client.last_event_ts > 15:
            try:
                client.finalize_from_history(prompt_id)
            except Exception:
                pass
    return "timeout"


def _sanitize_tasks(raw):
    """前端回传的任务清单做白名单清洗与上限校验。"""
    if not isinstance(raw, list) or not raw:
        raise ValueError("任务清单为空")
    if len(raw) > MAX_BATCH:
        raise ValueError(f"单批最多 {MAX_BATCH} 张")
    out = []
    for i, t in enumerate(raw):
        if not isinstance(t, dict):
            continue
        pipe = t.get("pipeline") if t.get("pipeline") in PIPELINES else "anima"
        try:
            w = max(64, min(int(t.get("w") or 832), 2048))
            h = max(64, min(int(t.get("h") or 1216), 2048))
            seed = int(t.get("seed")) if t.get("seed") is not None else secrets.randbits(31)
        except (TypeError, ValueError):
            w, h, seed = 832, 1216, secrets.randbits(31)
        prompt = str(t.get("prompt") or "").strip()
        if not prompt:
            continue
        out.append({
            "name": str(t.get("name") or f"任务 {i + 1}")[:80],
            "pipeline": pipe, "w": w, "h": h,
            "pos_prompt": str(t.get("pos_prompt") or "")[:400],
            "prompt": prompt[:2000],
            "neg": str(t.get("neg") or NEG_BASE)[:1000],
            "seed": seed % (2 ** 32),
            "batch": str(t.get("batch") or "")[:60],
            "category": str(t.get("category") or "")[:10],
        })
    if not out:
        raise ValueError("没有有效任务(缺提示词)")
    return out


def _prune_records():
    """批次出口清理旧记录(生成提交路径之外的第二条任务写入来源)。"""
    try:
        db.prune_tasks()
    except Exception as e:
        log(f"清理旧记录失败: {e}")


def engine(tasks):
    with _state_lock:
        STATE.update(running=True, stop=False, done=0, total=len(tasks), current="",
                     finished=False, err=0)
    log(f"批次开始,共 {len(tasks)} 张")
    groups = {}
    for t in tasks:
        groups.setdefault(t["pipeline"], []).append(t)
    last_pipe = None
    for pipe_key in PIPELINES:
        for t in groups.get(pipe_key, []):
            if STATE["stop"]:
                log("用户停止")
                with _state_lock:
                    STATE.update(running=False, finished=True)
                _prune_records()
                return
            if last_pipe and pipe_key != last_pipe:
                log(f"切换模型 {PIPELINES[last_pipe]['model']} -> {PIPELINES[pipe_key]['model']},释放显存")
                free_vram()
            last_pipe = pipe_key
            if gpu_temp_enabled():
                temp = _update_temp_display()
                while temp >= 72 and not STATE["stop"]:
                    wait_s = 180 if temp >= 78 else 60
                    log(f"GPU 温度 {temp}°C 偏高,休息 {wait_s}s")
                    time.sleep(wait_s)
                    temp = _update_temp_display()
            with _state_lock:
                STATE["current"] = f"{t['name']} [{pipe_key}]"
            try:
                prompt_id = client.submit(build_graph(pipe_key, t["prompt"], t["neg"],
                                                      t["seed"], t["w"], t["h"]))
                _insert_task(prompt_id, t)
                client.ensure_ws()
                status = _wait_task(prompt_id)
                if status == "done":
                    log(f"完成 {t['name']} [{pipe_key}]")
                elif status == "stopped":
                    log(f"停止 {t['name']}")
                else:
                    log(f"{'超时' if status == 'timeout' else '失败'} {t['name']} ({status})")
                    with _state_lock:
                        STATE["err"] += 1
            except Exception as e:  # 提交/落库任何异常都不中断批次
                log(f"失败 {t['name']}: {e}")
                with _state_lock:
                    STATE["err"] += 1
            with _state_lock:
                STATE["done"] += 1
    free_vram()
    with _state_lock:
        STATE.update(running=False, current="", finished=True)
    _prune_records()
    log(f"批次结束:完成 {STATE['done']}/{STATE['total']},失败 {STATE['err']}")


def start(raw_tasks):
    with _state_lock:
        if STATE["running"]:
            raise ValueError("有批次正在运行")
        STATE["running"] = True  # 先占位再启动线程,防并发双击同时起两个批次
    try:
        tasks = _sanitize_tasks(raw_tasks)
        client.queue()  # 提交前确认 ComfyUI 可达
    except Exception:
        with _state_lock:
            STATE["running"] = False
        raise
    threading.Thread(target=engine, args=(tasks,), daemon=True).start()
    return len(tasks)


def stop():
    STATE["stop"] = True
