"""ComfyUI 主机的 GPU/系统状态采集(comfyweb 与 ComfyUI 可能不在同一台机器)。

数据源分层,取到即用:
1. SSH(nvidia-smi):COMFY_SSH=user@host[:port] 时跨机取温度/利用率/显存,
   Windows/Linux 皆可(主机需开启 SSH、部署本机公钥,并先用 ssh-keyscan
   录入主机密钥——本模块不自动信任首次连接);
2. 本机 nvidia-smi:服务与 GPU 同机时;
3. ComfyUI /system_stats:跨机零配置,只有显存与内存,无温度/利用率。
"""
import os
import subprocess
import threading
import time

from comfy_client import client

_CACHE = {"ts": 0.0, "data": {}}
_LOCK = threading.Lock()
_TTL = 5.0


def ssh_target():
    """COMFY_SSH=user@host[:port] → (user@host, port);未配置返回 None。"""
    raw = (os.environ.get("COMFY_SSH") or "").strip()
    if not raw:
        return None
    host, _, port = raw.partition(":")
    return host, (port or "22")


def _parse_gpu_line(line):
    parts = [p.strip() for p in line.split(",")]
    return {"temp": int(parts[0]), "util": int(parts[1]),
            "vram_used": int(parts[2]), "vram_total": int(parts[3])}


def _ssh_run(cmd, port, timeout=10):
    host, _ = ssh_target()
    # 严格校验主机密钥,不做首次自动信任(accept-new 存在 TOFU 中间人窗口);
    # 首次部署先手动录入:ssh-keyscan -p <port> <host> >> ~/.ssh/known_hosts
    r = subprocess.run(
        ["ssh", "-p", port, "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
         "-o", "StrictHostKeyChecking=yes", host, cmd],
        capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "").strip()[:120] or f"ssh 退出码 {r.returncode}")
    return r.stdout


def _via_ssh(data):
    host, port = ssh_target()
    out = _ssh_run("nvidia-smi --query-gpu=temperature.gpu,utilization.gpu,"
                   "memory.used,memory.total --format=csv,noheader,nounits", port)
    data.update(_parse_gpu_line(out.strip().splitlines()[0]))
    data["src"] = "ssh"
    try:  # CPU 占用:Linux 走 /proc,Windows 走 wmic;都失败就跳过
        load = _ssh_run("cat /proc/loadavg", port).split()[0]
        data["load"] = float(load)
    except Exception:
        try:
            out = _ssh_run("wmic cpu get loadpercentage", port)
            digits = [l.strip() for l in out.splitlines() if l.strip().isdigit()]
            if digits:
                data["cpu"] = int(digits[-1])
        except Exception:
            pass


def _via_hwapi(data):
    """ComfyUI 插件 comfyui-hwapi 的 /hwapi:跨机取温度/利用率/显存,零配置首选。"""
    st = client.get_json("/hwapi")
    data.update(_map_hwapi(st))
    data["src"] = "hwapi"


def _map_hwapi(st):
    g = (st.get("gpus") or [{}])[0]
    out = {"temp": int(st.get("gpu_temperature") or g.get("temperature") or 0),
           "util": int(st.get("gpu_utilization") or g.get("utilization") or 0),
           "vram_used": int(st.get("vram_used") or g.get("vram_used") or 0) // (1024 * 1024),
           "vram_total": int(st.get("vram_total") or g.get("vram_total") or 0) // (1024 * 1024)}
    if st.get("cpu_temperature") is not None:
        out["cpu_temp"] = int(st["cpu_temperature"])
    return out


def _via_local(data):
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=temperature.gpu,utilization.gpu,"
         "memory.used,memory.total", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=5).stdout
    data.update(_parse_gpu_line(out.strip().splitlines()[0]))
    data["src"] = "local"


def _via_comfy(data):
    """ComfyUI 官方 /system_stats:跨机兜底,只有显存与内存。"""
    st = client.system_stats()
    dev = (st.get("devices") or [{}])[0]
    data["vram_used"] = int((dev.get("vram_total") or 0) - (dev.get("vram_free") or 0)) // (1024 * 1024)
    data["vram_total"] = int(dev.get("vram_total") or 0) // (1024 * 1024)
    data["src"] = "comfy"


def _fill_ram(data):
    if "ram_total" in data:
        return
    try:
        sysinfo = (client.system_stats() or {}).get("system") or {}
        if sysinfo.get("ram_total"):
            data["ram_total"] = int(sysinfo["ram_total"]) // (1024 * 1024)
            data["ram_free"] = int(sysinfo.get("ram_free") or 0) // (1024 * 1024)
    except Exception:
        pass


def fetch(force=False):
    """返回主机状态 dict(5s 缓存)。数据源优先级:
    SSH(温度/利用率/显存+CPU) → ComfyUI hwapi 插件(温度/利用率/显存,跨机零配置)
    → 本机 nvidia-smi → ComfyUI /system_stats(仅显存);内存从 system_stats 补齐。"""
    now = time.time()
    with _LOCK:
        if not force and now - _CACHE["ts"] < _TTL:
            return dict(_CACHE["data"])
    data = {}
    try:
        if ssh_target():
            _via_ssh(data)
        else:
            _via_hwapi(data)
    except Exception as e:
        data["err"] = str(e)[:120]
    if "temp" not in data:  # SSH/hwapi 都不可用时:同机走本机 nvidia-smi
        try:
            _via_local(data)
        except Exception as e:
            data["err"] = data.get("err") or str(e)[:120]
    if "vram_total" not in data:
        try:
            _via_comfy(data)
        except Exception as e:
            data["err"] = data.get("err") or f"ComfyUI {str(e)[:80]}"
    _fill_ram(data)
    with _LOCK:
        _CACHE["ts"] = now
        _CACHE["data"] = data
    return dict(data)


def gpu_temp():
    """批次引擎温度保护用:取到温度返回整数,取不到返回 0(视为不限制)。"""
    return fetch().get("temp", 0) or 0
