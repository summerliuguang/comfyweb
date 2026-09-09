"""工作流模板:解析 ComfyUI「API 格式」JSON,自动识别可调参数;提交时注入用户值。

模板文件(data/workflows/<id>.json)结构:
{
  "version": 1,
  "name": "SDXL 文生图",
  "workflow": {...API 格式 JSON...},
  "params": [ {param}, ... ],
  "batch_node": "5"          # EmptyLatentImage 类节点 id,数量注入 batch_size;无则 null
}

param 结构:
{
  "name": "3:seed",          # 唯一键 node_id:input
  "node_id": "3", "input": "seed",
  "label": "随机种子",
  "widget": "seed",          # textarea|text|number|float|select|toggle|seed
  "visible": true,           # 生成页是否显示
  "advanced": false,         # 归入「高级」折叠区
  "value": <默认值>,
  "min": 0, "max": 100, "step": 1,   # number/float 可用
  "options": [...],          # select 静态选项
  "dynamic": "checkpoints",  # select 动态选项:从 /models/<folder> 取
  "role": "positive"         # 正/负提示词标记
}
"""
import copy
import re
import secrets

PROMPT_CLASS = "CLIPTextEncode"
SAMPLER_CLASSES = {"KSampler", "KSamplerAdvanced"}
CHECKPOINT_CLASSES = {"CheckpointLoaderSimple": "checkpoints", "UNETLoader": "diffusion_models",
                      "ImageOnlyCheckpointLoader": "checkpoints"}
LORA_CLASSES = {"LoraLoader", "LoraLoaderModelOnly"}
VAE_CLASSES = {"VAELoader"}
LATENT_CLASSES = {"EmptyLatentImage", "EmptySD3LatentImage", "EmptyHunyuanLatentVideo"}
SEED_INPUTS = {"seed", "noise_seed"}
UI_SKIP_TYPES = {"Note", "MarkdownNote"}   # 纯前端便签节点,无执行语义
PASSTHROUGH_TYPES = {"Reroute"}            # 前端重定向节点:输入直通输出


class WorkflowParseError(ValueError):
    pass


WIDGET_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO"}


def _widget_inputs(info):
    """按 object_info 的 input_order 返回 (name -> (类型, 元数据)) 的 widget 输入。"""
    inputs = info.get("input") or {}
    req = inputs.get("required") or {}
    opt = inputs.get("optional") or {}
    iorder = info.get("input_order") or {}
    names = list(iorder.get("required") or req.keys())
    names += [n for n in (iorder.get("optional") or opt.keys()) if n not in req]
    out = {}
    for n in names:
        spec = req.get(n) or opt.get(n)
        if not isinstance(spec, list) or not spec:
            continue
        s0 = spec[0]
        if isinstance(s0, list) or s0 in WIDGET_TYPES:
            meta = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
            out[n] = (s0, meta)
    return out


def _cast(s0, val):
    try:
        if s0 == "INT":
            return int(float(val))
        if s0 == "FLOAT":
            return float(val)
        if s0 == "BOOLEAN":
            return bool(val)
        if s0 == "STRING":
            return str(val)
    except (TypeError, ValueError):
        return val
    return val


def _norm_combo_str(val):
    """Windows 上 ComfyUI 保存 UI JSON 会把子目录路径存成双反斜杠,归一为单反斜杠。"""
    if isinstance(val, str):
        return re.sub(r"\\{2,}", lambda m: "\\", val)
    return val


def ui_to_api(uiwf, object_info):
    """把 ComfyUI「保存」的 UI 格式工作流转成可提交的 API 格式。

    object_info: {class_type: info},需覆盖工作流里用到的全部可执行节点类。
    支持:bypass/静音节点按 ComfyUI 语义直通(输出取同类型输入的来源)、Reroute、
    PrimitiveNode 常量注入、字典型 widgets_values。转换失败抛 WorkflowParseError。
    """
    if not isinstance(uiwf, dict) or not isinstance(uiwf.get("nodes"), list):
        raise WorkflowParseError("不是 UI 格式的工作流 JSON")
    if (uiwf.get("definitions") or {}).get("subgraphs"):
        raise WorkflowParseError(
            "该工作流包含子图(subgraph),请在 ComfyUI 里用「导出(API)」后再粘贴导入")

    # link_id -> (源节点id, 输出槽)
    links = {}
    for l in uiwf.get("links") or []:
        if isinstance(l, list) and len(l) >= 4:
            links[l[0]] = (str(l[1]), l[2])
    nodes = {}
    for n in uiwf.get("nodes") or []:
        if isinstance(n, dict) and n.get("id") is not None:
            nodes[str(n["id"])] = n

    def find_passthrough_input(node, out_slot):
        """bypass/静音/Reroute 节点:找与该输出同类型、且已连线的输入。"""
        outs = node.get("outputs") or []
        out_type = outs[out_slot].get("type") if out_slot < len(outs) else None
        inputs = node.get("inputs") or []
        if node.get("type") == "Reroute":
            for inp in inputs:
                if inp.get("link") is not None:
                    return inp
            return None
        for inp in inputs:
            if out_type and inp.get("type") == out_type and inp.get("link") is not None:
                return inp
        for inp in inputs:  # 容错:类型标注缺失时退化为第一个有连线的输入
            if inp.get("link") is not None:
                return inp
        return None

    def resolve_link(link_id, seen):
        """把 link_id 解析为 ('node', 节点id, 槽) 或 ('value', 常量)。"""
        if link_id in seen:
            raise WorkflowParseError("工作流连线存在环,无法解析")
        seen = seen | {link_id}
        ref = links.get(link_id)
        if ref is None:
            raise WorkflowParseError(f"连线 {link_id} 找不到源头")
        src_id, slot = ref
        node = nodes.get(src_id)
        if node is None:
            raise WorkflowParseError(f"连线 {link_id} 指向不存在的节点 {src_id}")
        cls = node.get("type")
        mode = node.get("mode", 0)
        title = node.get("title") or cls
        if cls in PASSTHROUGH_TYPES or mode in (2, 4):
            inp = find_passthrough_input(node, slot)
            if inp is None or inp.get("link") is None:
                raise WorkflowParseError(
                    f"节点「{title}」被 bypass/静音,其输出找不到可直通的输入来源;"
                    f"请先在 ComfyUI 里恢复或删除该节点,或改用「导出(API)」")
            return resolve_link(inp["link"], seen)
        if cls == "PrimitiveNode":
            wv = node.get("widgets_values") or []
            return ("value", wv[0] if wv else None)
        return ("node", src_id, slot)

    api = {}
    for nid, node in nodes.items():
        cls = node.get("type")
        mode = node.get("mode", 0)
        title = node.get("title") or cls
        if cls in UI_SKIP_TYPES or cls in PASSTHROUGH_TYPES or mode in (2, 4):
            continue  # 便签/重定向/bypass 不进入执行图,连线在 resolve_link 里绕行
        info = (object_info or {}).get(cls)
        if not info:
            raise WorkflowParseError(
                f"节点「{title}」({cls}) 在 ComfyUI 中不存在——可能缺少对应插件,或 ComfyUI 未连接")
        specs = _widget_inputs(info)

        widgets = node.get("widgets_values")
        widget_order, link_refs = [], {}
        has_markers = any("widget" in i for i in node.get("inputs") or [])
        for inp in node.get("inputs") or []:
            if inp.get("link") is not None:
                link_refs[inp["name"]] = inp["link"]
            elif has_markers and "widget" in inp:
                widget_order.append(inp["name"])
        if not has_markers and not isinstance(widgets, dict):
            # 旧版格式:inputs 只含连线,widget 顺序按 object_info
            widget_order = list(specs.keys())

        values = {}
        if isinstance(widgets, dict):
            # 新版部分节点(如 VHS_VideoCombine)按键名保存,逐名对位最稳
            for name, spec in specs.items():
                if name in widgets:
                    s0, meta = spec
                    v = widgets[name]
                    if isinstance(s0, list):
                        v = _norm_combo_str(v)
                    casted = _cast(s0, v)
                    if casted in ("", None) and s0 in ("INT", "FLOAT"):
                        casted = meta.get("default")
                    if casted in ("", None):
                        continue
                    values[name] = casted
        else:
            widgets = list(widgets or [])
            wi = 0
            for name in widget_order:
                spec = specs.get(name)
                if spec is None:
                    # 前端专属控件(如 LoadImage 的 upload 按钮):不参与执行,
                    # 但要消耗一个位置值,保证后续参数不错位
                    wi += 1
                    continue
                if wi >= len(widgets):
                    raise WorkflowParseError(
                        f"节点「{title}」的参数值数量不足({name}),请用「导出(API)」")
                s0, meta = spec
                val = widgets[wi]
                wi += 1
                if meta.get("control_after_generate") and wi < len(widgets) \
                        and isinstance(widgets[wi], str):
                    wi += 1
                if isinstance(s0, list):
                    val = _norm_combo_str(val)
                casted = _cast(s0, val)
                if casted in ("", None) and s0 in ("INT", "FLOAT"):
                    casted = meta.get("default")
                if casted in ("", None):
                    continue
                values[name] = casted

        inputs = {}
        for name, link_id in link_refs.items():
            kind = resolve_link(link_id, set())
            if kind[0] == "value":
                spec = _widget_inputs(info).get(name)
                inputs[name] = _cast(spec[0], kind[1]) if spec else kind[1]
            else:
                inputs[name] = [kind[1], kind[2]]
        inputs.update(values)

        spec_node = {"class_type": cls, "inputs": inputs}
        if node.get("title"):
            spec_node["_meta"] = {"title": node["title"]}
        api[nid] = spec_node

    # 校验连线目标都存在
    for nid, spec in api.items():
        for name, v in spec["inputs"].items():
            if isinstance(v, list) and v[0] not in api:
                raise WorkflowParseError(
                    f"节点 {nid} 的 {name} 引用了不存在的节点 {v[0]}(可能已被 bypass 或删除)")

    return api


def _meta_title(spec):
    return ((spec.get("_meta") or {}).get("title") or "").strip()


def parse_workflow(wf, object_info=None):
    """解析 API 格式工作流,返回模板数据(不含 name)。"""
    if not isinstance(wf, dict) or not wf:
        raise WorkflowParseError("不是有效的工作流 JSON")
    if isinstance(wf.get("nodes"), list):
        raise WorkflowParseError(
            "检测到 UI 格式工作流。请在 ComfyUI 里通过菜单「工作流 → 导出(API)」"
            "保存 API 格式 JSON 后再导入"
        )
    nodes = {str(nid): spec for nid, spec in wf.items()
             if isinstance(spec, dict) and spec.get("class_type")}
    if not nodes:
        raise WorkflowParseError("工作流里没有可识别的节点")

    def input_meta(cls, name):
        info = (object_info or {}).get(cls) or {}
        inputs = info.get("input") or {}
        req = inputs.get("required") or {}
        opt = inputs.get("optional") or {}
        return req.get(name) or opt.get(name)

    # 找出接在采样器 positive/negative 输入上的 CLIPTextEncode 文本框
    prompt_roles = {}
    for spec in nodes.values():
        inputs = spec.get("inputs") or {}
        for role in ("positive", "negative"):
            link = inputs.get(role)
            if isinstance(link, list) and len(link) >= 2:
                src = nodes.get(str(link[0]))
                if src and src.get("class_type") == PROMPT_CLASS:
                    prompt_roles[(str(link[0]), "text")] = role

    used_labels = set()

    def unique_label(base):
        label, i = base, 2
        while label in used_labels:
            label = f"{base}{i}"
            i += 1
        used_labels.add(label)
        return label

    params = []
    batch_node = None

    def add(nid, name, value, **kw):
        p = {"name": f"{nid}:{name}", "node_id": nid, "input": name,
             "label": name, "widget": "text", "visible": False,
             "advanced": False, "value": value}
        p.update(kw)
        params.append(p)

    for nid, spec in nodes.items():
        cls = spec.get("class_type")
        inputs = spec.get("inputs") or {}
        title = _meta_title(spec)
        for name, value in inputs.items():
            if isinstance(value, list):
                continue  # 节点连线,不是可编辑输入
            key = f"{nid}:{name}"
            role = prompt_roles.get((nid, name))

            if role == "positive":
                add(nid, name, value, label=unique_label(title or "正面提示词"),
                    widget="textarea", visible=True, role="positive")
            elif role == "negative":
                add(nid, name, value, label=unique_label(title or "负面提示词"),
                    widget="textarea", visible=True, role="negative")
            elif cls == PROMPT_CLASS and name == "text":
                add(nid, name, value, label=unique_label(title or "提示词"),
                    widget="textarea", visible=True)
            elif cls in CHECKPOINT_CLASSES and name in ("ckpt_name", "unet_name"):
                add(nid, name, value, label=unique_label(title or "模型"), widget="select",
                    dynamic=CHECKPOINT_CLASSES[cls], visible=True)
            elif cls in VAE_CLASSES and name == "vae_name":
                add(nid, name, value, label=unique_label(title or "VAE"), widget="select",
                    dynamic="vae", visible=True)
            elif cls in LORA_CLASSES and name == "lora_name":
                add(nid, name, value, label=unique_label(title or "LoRA"), widget="select",
                    dynamic="loras", visible=True)
            elif cls in LORA_CLASSES and name.startswith("strength_"):
                add(nid, name, value, label="LoRA 强度" if name == "strength_model" else "LoRA 文本强度",
                    widget="float", min=-5.0, max=5.0, step=0.05, visible=True)
            elif cls in SAMPLER_CLASSES and name in SEED_INPUTS:
                add(nid, name, value, label="随机种子", widget="seed", visible=True)
            elif cls in SAMPLER_CLASSES and name == "steps":
                add(nid, name, value, label="步数", widget="number", min=1, max=150,
                    step=1, visible=True)
            elif cls in SAMPLER_CLASSES and name == "cfg":
                add(nid, name, value, label="CFG", widget="float", min=0, max=30,
                    step=0.5, visible=True)
            elif cls in SAMPLER_CLASSES and name == "sampler_name":
                add(nid, name, value, label="采样器", widget="select", visible=True,
                    options=_combo_from_object_info(object_info, cls, name))
            elif cls in SAMPLER_CLASSES and name == "scheduler":
                add(nid, name, value, label="调度器", widget="select", visible=True,
                    options=_combo_from_object_info(object_info, cls, name))
            elif cls in SAMPLER_CLASSES and name == "denoise":
                add(nid, name, value, label="降噪幅度", widget="float", min=0, max=1,
                    step=0.05, visible=True)
            elif cls in SAMPLER_CLASSES and name in ("start_at_step", "end_at_step"):
                add(nid, name, value, widget="number", min=0, max=10000, step=1,
                    label={"start_at_step": "起始步", "end_at_step": "结束步"}[name])
            elif cls in SAMPLER_CLASSES and name == "add_noise":
                add(nid, name, value, widget="toggle", label="添加噪声")
            elif cls in LATENT_CLASSES and name == "batch_size":
                batch_node = nid  # 数量由全局「生成数量」控制,不作为参数
            elif cls in LATENT_CLASSES and name in ("width", "height"):
                add(nid, name, value, label="宽度" if name == "width" else "高度",
                    widget="number", min=64, max=8192, step=8, visible=True)
            else:
                # 未知输入:按 object_info 类型推断,归入高级区默认隐藏
                add(nid, name, value, label=unique_label(title or f"节点 {nid}"),
                    **_infer_widget(cls, name, value, input_meta))

    sort_params(params)
    return {"workflow": wf, "params": params, "batch_node": batch_node,
            "node_count": len(nodes)}


def sort_params(params):
    """表单排序:提示词 → 模型/LoRA → 采样参数 → 尺寸,其余保持原顺序(稳定排序)。"""
    def sort_key(p):
        if p.get("role") == "positive":
            return 0
        if p.get("role") == "negative":
            return 1
        if p.get("dynamic") == "checkpoints":
            return 2
        if p.get("dynamic") == "loras":
            return 3
        if p.get("widget") == "seed":
            return 4
        order = {"steps": 5, "cfg": 6, "sampler_name": 7, "scheduler": 8,
                 "denoise": 9, "width": 10, "height": 11}
        return order.get(p["input"], 50)

    params.sort(key=sort_key)


def combo_options(object_info, cls, name):
    """从 object_info 里取某节点某输入的下拉选项;没有则返回空列表。"""
    return _combo_from_object_info(object_info, cls, name)


def input_default(object_info, cls, name):
    """取 object_info 里某数值/布尔输入的声明默认值;没有返回 None。"""
    info = (object_info or {}).get(cls) or {}
    inputs = info.get("input") or {}
    spec = (inputs.get("required") or {}).get(name) or (inputs.get("optional") or {}).get(name)
    if isinstance(spec, list) and len(spec) > 1 and isinstance(spec[1], dict):
        return spec[1].get("default")
    return None


def _combo_from_object_info(object_info, cls, name):
    options = _infer_widget(cls, name, None, lambda c, n: _raw_meta(object_info, c, n))
    return options.get("options") or []


def _raw_meta(object_info, cls, name):
    info = (object_info or {}).get(cls) or {}
    inputs = info.get("input") or {}
    req = inputs.get("required") or {}
    opt = inputs.get("optional") or {}
    return req.get(name) or opt.get(name)


def _infer_widget(cls, name, value, meta_of):
    """根据 object_info 元数据或值类型推断控件。返回 add() 的 kwargs。"""
    meta = meta_of(cls, name)
    if isinstance(meta, list) and meta:
        spec0 = meta[0]
        extra = meta[1] if len(meta) > 1 and isinstance(meta[1], dict) else {}
        if isinstance(spec0, list):
            return {"widget": "select", "options": [str(o) for o in spec0], "advanced": True}
        if spec0 == "INT":
            return {"widget": "number", "min": extra.get("min"), "max": extra.get("max"),
                    "step": extra.get("step", 1), "advanced": True}
        if spec0 == "FLOAT":
            return {"widget": "float", "min": extra.get("min"), "max": extra.get("max"),
                    "step": extra.get("step"), "advanced": True}
        if spec0 == "BOOLEAN":
            return {"widget": "toggle", "advanced": True}
        if spec0 == "STRING":
            w = "textarea" if extra.get("multiline") else "text"
            return {"widget": w, "advanced": True}
    if isinstance(value, bool):
        return {"widget": "toggle", "advanced": True}
    if isinstance(value, int):
        return {"widget": "number", "advanced": True}
    if isinstance(value, float):
        return {"widget": "float", "advanced": True}
    return {"widget": "text", "advanced": True}


def coerce_value(p, raw):
    """按控件类型把用户输入转成合法值;非法时回退默认值。"""
    widget = p.get("widget")
    default = p.get("value")
    try:
        if widget in ("number", "seed"):
            v = int(float(raw))
        elif widget == "float":
            v = float(raw)
        elif widget == "toggle":
            v = bool(raw) if not isinstance(raw, str) else raw.lower() in ("1", "true", "on")
        elif raw is None:
            return default
        else:
            v = str(raw)
    except (TypeError, ValueError):
        return default
    if widget in ("number", "seed"):
        lo, hi = p.get("min"), p.get("max")
        if lo is not None:
            v = max(int(lo), v)
        if hi is not None:
            v = min(int(hi), v)
    elif widget == "float":
        lo, hi = p.get("min"), p.get("max")
        if lo is not None:
            v = max(float(lo), v)
        if hi is not None:
            v = min(float(hi), v)
    return v


def build_prompt(template, values, count=None, random_seed=True):
    """按模板和用户值构建可提交的 API prompt。

    返回 (prompt, prompt_text, seed)。seed 为本次实际使用的种子(无种子参数为 None)。
    """
    prompt = copy.deepcopy(template["workflow"])
    params = template.get("params") or []
    prompt_text = ""
    seed = None
    for p in params:
        raw = values.get(p["name"], p.get("value"))
        if p.get("widget") == "seed":
            if random_seed:
                raw = secrets.randbits(48)
            raw = coerce_value(p, raw)
            seed = raw
        val = coerce_value(p, raw)
        if p.get("widget") == "select" and isinstance(val, str):
            val = _norm_combo_str(val)  # 旧模板里可能残留双反斜杠的子目录路径
        node = prompt.get(p["node_id"])
        if node is not None and p["input"] in (node.get("inputs") or {}):
            node["inputs"][p["input"]] = val
        if p.get("role") == "positive" and isinstance(val, str):
            prompt_text = val
    if count is not None and template.get("batch_node"):
        node = prompt.get(str(template["batch_node"]))
        if node is not None and "batch_size" in (node.get("inputs") or {}):
            node["inputs"]["batch_size"] = int(count)
    if not prompt_text:
        # 工作流的提示词节点不直接接采样器时(中间有其他节点),取第一个可见文本域兜底
        for p in params:
            if p.get("visible") and p.get("widget") == "textarea":
                v = values.get(p["name"], p.get("value"))
                if isinstance(v, str) and v:
                    prompt_text = v
                    break
    return prompt, prompt_text, seed
