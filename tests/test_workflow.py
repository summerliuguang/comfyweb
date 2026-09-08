"""workflow.py 单元测试:转换器、解析、值注入、消毒。"""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import civitai
import workflow
from comfy_client import format_prompt_error

# 与真实 UI 导出一致的最小工作流(含 outputs/type 字段与 control_after_generate 附加值)
UI_WF = {
    "last_node_id": 7, "last_link_id": 4, "version": 0.4,
    "nodes": [
        {"id": 3, "type": "KSampler", "mode": 0, "title": "采样",
         "inputs": [{"name": "model", "type": "MODEL", "link": 1},
                    {"name": "positive", "type": "CONDITIONING", "link": 2},
                    {"name": "negative", "type": "CONDITIONING", "link": 3},
                    {"name": "latent_image", "type": "LATENT", "link": None},
                    {"name": "seed", "widget": {"name": "seed"}},
                    {"name": "steps", "widget": {"name": "steps"}},
                    {"name": "cfg", "widget": {"name": "cfg"}},
                    {"name": "sampler_name", "widget": {"name": "sampler_name"}},
                    {"name": "scheduler", "widget": {"name": "scheduler"}},
                    {"name": "denoise", "widget": {"name": "denoise"}}],
         "outputs": [],
         "widgets_values": [8566257, "randomize", 20, 8, "euler", "normal", 1]},
        {"id": 4, "type": "CheckpointLoaderSimple", "mode": 0,
         "inputs": [{"name": "ckpt_name", "widget": {"name": "ckpt_name"}}],
         "outputs": [{"name": "MODEL", "type": "MODEL", "links": [1]},
                     {"name": "CLIP", "type": "CLIP", "links": [4, 5]},
                     {"name": "VAE", "type": "VAE", "links": []}],
         "widgets_values": ["v1-5-pruned.safetensors"]},
        {"id": 5, "type": "CLIPTextEncode", "mode": 0, "title": "正面",
         "inputs": [{"name": "clip", "type": "CLIP", "link": 4},
                    {"name": "text", "widget": {"name": "text"}}],
         "outputs": [{"name": "CONDITIONING", "type": "CONDITIONING", "links": [2]}],
         "widgets_values": ["masterpiece girl"]},
        {"id": 7, "type": "CLIPTextEncode", "mode": 0, "title": "负面",
         "inputs": [{"name": "clip", "type": "CLIP", "link": 5},
                    {"name": "text", "widget": {"name": "text"}}],
         "outputs": [{"name": "CONDITIONING", "type": "CONDITIONING", "links": [3]}],
         "widgets_values": ["bad hands"]},
    ],
    "links": [[1, 4, 0, 3, 0, "MODEL"], [2, 5, 0, 3, 1, "CONDITIONING"],
              [3, 7, 0, 3, 2, "CONDITIONING"], [4, 4, 1, 5, 0, "CLIP"],
              [5, 4, 1, 7, 0, "CLIP"]],
}
OI = {
    "KSampler": {"input": {"required": {
        "model": ["MODEL", {}], "positive": ["CONDITIONING", {}],
        "negative": ["CONDITIONING", {}], "latent_image": ["LATENT", {}],
        "seed": ["INT", {"min": 0, "max": 2 ** 64 - 1, "control_after_generate": True}],
        "steps": ["INT", {"min": 1, "max": 10000}],
        "cfg": ["FLOAT", {"min": 0.0, "max": 100.0}],
        "sampler_name": [["euler", "dpmpp_2m"], {}],
        "scheduler": [["normal", "karras"], {}],
        "denoise": ["FLOAT", {"min": 0.0, "max": 1.0}]}},
        "input_order": {"required": ["model", "seed", "steps", "cfg", "sampler_name",
                                     "scheduler", "denoise", "positive", "negative",
                                     "latent_image"]},
        "name": "KSampler"},
    "CheckpointLoaderSimple": {"input": {"required": {
        "ckpt_name": [["v1-5-pruned.safetensors"], {}]}}, "name": "CheckpointLoaderSimple"},
    "CLIPTextEncode": {"input": {"required": {
        "text": ["STRING", {"multiline": True}], "clip": ["CLIP", {}]}}, "name": "CLIPTextEncode"},
    "EmptyLatentImage": {"input": {"required": {
        "width": ["INT", {"min": 64, "max": 8192, "step": 8}],
        "height": ["INT", {"min": 64, "max": 8192, "step": 8}],
        "batch_size": ["INT", {"min": 1, "max": 4096}]}}, "name": "EmptyLatentImage"},
    "SaveImage": {"input": {"required": {
        "filename_prefix": ["STRING", {"default": "ComfyUI"}],
        "images": ["IMAGE", {}]}}, "name": "SaveImage"},
}


class TestUiToApi(unittest.TestCase):
    def test_convert(self):
        api = workflow.ui_to_api(UI_WF, OI)
        ks = api["3"]
        # control_after_generate('randomize') 被跳过,各值对位正确
        self.assertEqual(ks["inputs"]["seed"], 8566257)
        self.assertEqual(ks["inputs"]["steps"], 20)
        self.assertEqual(ks["inputs"]["sampler_name"], "euler")
        # 连线解析成 [源节点id, 输出槽]
        self.assertEqual(ks["inputs"]["positive"], ["5", 0])
        self.assertEqual(ks["inputs"]["model"], ["4", 0])
        self.assertEqual(api["4"]["inputs"]["ckpt_name"], "v1-5-pruned.safetensors")
        self.assertEqual(api["5"]["inputs"]["text"], "masterpiece girl")

    def test_ui_format_rejected(self):
        with self.assertRaises(workflow.WorkflowParseError):
            workflow.parse_workflow({"nodes": [], "links": []})
        subgraph = dict(UI_WF)
        subgraph["definitions"] = {"subgraphs": [{"id": "s1", "nodes": []}]}
        with self.assertRaises(workflow.WorkflowParseError):
            workflow.ui_to_api(subgraph, OI)

    def test_bypass_passthrough(self):
        """bypass 的 CLIPTextEncode:其输出按类型直通到它 CLIP 输入的来源。"""
        wf = copy.deepcopy(UI_WF)
        wf["nodes"][2]["mode"] = 4  # 正面提示词节点 bypass
        api = workflow.ui_to_api(wf, OI)
        self.assertNotIn("5", api)  # bypass 节点不进入执行图
        self.assertEqual(api["3"]["inputs"]["positive"], ["4", 1])

    def test_note_and_reroute(self):
        wf = copy.deepcopy(UI_WF)
        wf["nodes"].append({"id": 9, "type": "Note", "mode": 0, "inputs": [],
                            "widgets_values": ["备忘"]})
        wf["nodes"].append({"id": 10, "type": "Reroute", "mode": 0,
                            "inputs": [{"name": "", "type": "MODEL", "link": 1}],
                            "outputs": [{"name": "", "type": "MODEL", "links": [1]}],
                            "widgets_values": []})
        wf["links"][0][2] = 10  # KSampler.model 改接 Reroute
        wf["links"].append([1, 4, 0, 10, 0, "MODEL"])
        api = workflow.ui_to_api(wf, OI)
        self.assertNotIn("9", api)   # 便签跳过
        self.assertNotIn("10", api)  # Reroute 不进入执行图
        self.assertEqual(api["3"]["inputs"]["model"], ["4", 0])  # 连线绕过 Reroute

    def test_dict_widgets(self):
        wf = copy.deepcopy(UI_WF)
        wf["nodes"][1]["widgets_values"] = {"ckpt_name": "v1-5-pruned.safetensors",
                                            "upload": "image"}  # 字典型(含前端控件)
        api = workflow.ui_to_api(wf, OI)
        self.assertEqual(api["4"]["inputs"]["ckpt_name"], "v1-5-pruned.safetensors")

    def test_frontend_widget_skipped(self):
        """object_info 里不存在的前端控件(upload 等)不进 prompt,但不错位。"""
        wf = copy.deepcopy(UI_WF)
        n3 = wf["nodes"][0]
        n3["inputs"].insert(4, {"name": "upload", "widget": {"name": "upload"}})
        n3["widgets_values"] = [8566257, "randomize", "image", 20, 8, "euler", "normal", 1]
        api = workflow.ui_to_api(wf, OI)
        self.assertNotIn("upload", api["3"]["inputs"])
        self.assertEqual(api["3"]["inputs"]["steps"], 20)


class TestParseAndBuild(unittest.TestCase):
    def setUp(self):
        api = workflow.ui_to_api(UI_WF, OI)
        api["6"] = {"class_type": "EmptyLatentImage",
                    "inputs": {"width": 512, "height": 512, "batch_size": 1}}
        self.tpl = workflow.parse_workflow(api, OI)

    def test_detect_and_sort(self):
        labels = [p["label"] for p in self.tpl["params"] if p["visible"]]
        self.assertEqual(labels[0], "正面")
        # 节点标题优先:标题为「负面」的节点直接用标题当标签
        self.assertEqual(labels[1], "负面")
        # 正面提示词的 role 由采样器连线判定
        pos = next(p for p in self.tpl["params"] if p.get("role") == "positive")
        self.assertEqual(pos["node_id"], "5")
        self.assertEqual(self.tpl["batch_node"], "6")

    def test_build_and_seed(self):
        prompt, text, seed = workflow.build_prompt(
            self.tpl, {"5:text": "a cat", "3:seed": 111}, random_seed=False)
        self.assertEqual(prompt["5"]["inputs"]["text"], "a cat")
        self.assertEqual(prompt["3"]["inputs"]["seed"], 111)
        self.assertEqual(seed, 111)
        p2, _, s2 = workflow.build_prompt(self.tpl, {}, random_seed=True)
        self.assertTrue(0 <= p2["3"]["inputs"]["seed"] <= 2 ** 48)
        self.assertEqual(p2["6"]["inputs"]["width"], 512)

    def test_coerce_clamp(self):
        self.assertEqual(workflow.coerce_value(
            {"widget": "number", "value": 512, "min": 64, "max": 8192}, 99999), 8192)
        self.assertEqual(workflow.coerce_value({"widget": "toggle", "value": True}, "false"), False)
        self.assertEqual(workflow.coerce_value({"widget": "float", "value": 1.0}, "bad"), 1.0)


class TestSanitizeAndError(unittest.TestCase):
    def test_sanitize_html(self):
        dirty = ('<p onclick="x()">ok</p><script>alert(1)</script>'
                 '<img src="https://image.civitai.com/a.jpg" onerror="x()">'
                 '<a href="javascript:bad()">x</a><a href="/m/1">link</a>')
        clean = civitai.sanitize_html(dirty)
        self.assertNotIn("script", clean)
        self.assertNotIn("onclick", clean)
        self.assertNotIn("onerror", clean)
        self.assertNotIn("javascript:", clean)
        self.assertIn('<p>ok</p>', clean)
        self.assertIn("https://civitai.com/m/1", clean)

    def test_prompt_error_format(self):
        msg = format_prompt_error({
            "error": {"message": "Prompt has no outputs"},
            "node_errors": {"4": {"class_type": "KSampler",
                                  "errors": [{"message": "bad seed", "details": "x"}]}}})
        self.assertIn("Prompt has no outputs", msg)
        self.assertIn("节点 4(KSampler)", msg)


if __name__ == "__main__":
    unittest.main()
