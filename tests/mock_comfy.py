"""测试用 mock ComfyUI:实现 comfyweb 依赖的 HTTP 端点(无 WebSocket)。

供 tests/test_api_smoke.py 在进程内以线程方式启动,也可单独运行:
    shared-venv/bin/python tests/mock_comfy.py   # 监听 127.0.0.1:5099
"""
import io
import threading
import time
import uuid

from flask import Flask, jsonify, request, send_file
from PIL import Image

app = Flask(__name__)
HISTORY = {}
PNG = None

OBJECT_INFO = {
    "KSampler": {"input": {"required": {
        "sampler_name": [["euler", "dpmpp_2m", "uni_pc"], {}],
        "scheduler": [["normal", "karras", "beta"], {}],
        "seed": ["INT", {"min": 0, "max": 2 ** 64 - 1, "control_after_generate": True}],
        "steps": ["INT", {"min": 1, "max": 10000, "default": 20}],
        "cfg": ["FLOAT", {"min": 0.0, "max": 100.0, "default": 8.0}],
        "denoise": ["FLOAT", {"min": 0.0, "max": 1.0, "default": 1.0}],
    }}, "name": "KSampler"},
    "CheckpointLoaderSimple": {"input": {"required": {
        "ckpt_name": [["v1-5-pruned.safetensors"], {}]}}, "name": "CheckpointLoaderSimple"},
    "EmptyLatentImage": {"input": {"required": {
        "width": ["INT", {"min": 64, "max": 8192, "step": 8}],
        "height": ["INT", {"min": 64, "max": 8192, "step": 8}],
        "batch_size": ["INT", {"min": 1, "max": 4096, "default": 1}]}},
        "name": "EmptyLatentImage"},
    "CLIPTextEncode": {"input": {"required": {
        "text": ["STRING", {"multiline": True}], "clip": ["CLIP", {}]}}, "name": "CLIPTextEncode"},
    "SaveImage": {"input": {"required": {
        "filename_prefix": ["STRING", {"default": "ComfyUI"}],
        "images": ["IMAGE", {}]}}, "name": "SaveImage"},
}


def make_png():
    global PNG
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 120, 40)).save(buf, "PNG")
    PNG = buf.getvalue()


@app.post("/prompt")
def prompt():
    body = request.get_json(force=True)
    if not isinstance(body.get("prompt"), dict):
        return jsonify({"error": {"type": "no_prompt", "message": "no prompt"}}), 400
    pid = str(uuid.uuid4())
    HISTORY[pid] = {"ready_at": time.time() + 2.0}
    return jsonify({"prompt_id": pid, "number": len(HISTORY), "node_errors": {}})


@app.get("/history/<pid>")
def history(pid):
    h = HISTORY.get(pid)
    if not h or time.time() < h["ready_at"]:
        return jsonify({})
    return jsonify({pid: {
        "prompt": [1, pid, {}, {}, []],
        "outputs": {"9": {"images": [
            {"filename": f"{pid}_00001_.png", "subfolder": "", "type": "output"},
            {"filename": f"{pid}_00002_.png", "subfolder": "", "type": "output"},
        ]}},
        "status": {"status_str": "success", "completed": True, "messages": []},
    }})


@app.get("/queue")
def queue():
    return jsonify({"queue_running": [], "queue_pending": []})


@app.get("/view")
def view():
    return send_file(io.BytesIO(PNG or make_png()), mimetype="image/png")


@app.get("/system_stats")
def stats():
    return jsonify({"system": {"comfyui_version": "mock-0.3", "python_version": "3.11"},
                    "devices": [{"name": "Mock GPU", "vram_total": 8 * 1024 ** 3,
                                 "vram_free": 6 * 1024 ** 3}]})


@app.get("/models/<folder>")
def models(folder):
    return jsonify({
        "checkpoints": ["dreamshaperXL.safetensors", "v1-5-pruned.safetensors"],
        "loras": ["add_detail.safetensors"],
        "vae": ["vae-ft-mse.safetensors"],
        "diffusion_models": [],
    }.get(folder, []))


@app.get("/object_info/<cls>")
def object_info(cls):
    info = OBJECT_INFO.get(cls)
    return jsonify({cls: info} if info is not None else {})


@app.post("/interrupt")
@app.post("/queue")
def misc():
    return "ok"


def start(port=5099):
    """后台线程启动 mock,供单元测试使用。"""
    make_png()
    t = threading.Thread(target=lambda: app.run(host="127.0.0.1", port=port, threaded=True),
                         daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    run()
