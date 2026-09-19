#!/usr/bin/env bash
# ComfyWeb 交互式安装:虚拟环境 + 依赖 + .env + 初始配置。
# systemd / nginx 不自动配置——需要开机自启时运行 deploy/systemd-gen.sh 生成单元后自行安装。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "=== ComfyWeb 安装 ==="
echo "安装目录: $ROOT"

# 1) Python 检查与虚拟环境
if ! command -v python3 >/dev/null; then
    echo "错误: 未找到 python3(需要 3.10+)" >&2
    exit 1
fi
if [ ! -x venv/bin/python ]; then
    echo "--- 创建虚拟环境 venv/"
    python3 -m venv venv
fi
echo "--- 安装依赖(境外源超时可加前缀: HTTPS_PROXY=http://代理:端口)"
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt
echo "依赖安装完成。"

# 2) .env
if [ ! -f .env ]; then
    cp .env.example .env
    echo "--- 已从模板创建 .env(AI 网关密钥等可选配置按需填写)"
else
    echo "--- .env 已存在,保留不动"
fi

# 3) ComfyUI 地址(http/https)
read -rp "ComfyUI 走 http 还是 https? [1=http(默认)/2=https]: " proto
case "$proto" in
    2) SCHEME="https" ;;
    *) SCHEME="http" ;;
esac
read -rp "ComfyUI 地址(主机:端口,如 192.168.1.10:8188): " hostport
while [ -z "${hostport}" ]; do
    read -rp "不能为空,请输入 ComfyUI 主机:端口: " hostport
done
COMFY_URL="$SCHEME://$hostport"
if [ "$SCHEME" = "https" ]; then
    read -rp "证书是自签的吗? [y/N]: " insecure
    if [[ "$insecure" =~ ^[Yy] ]]; then
        grep -q '^COMFYUI_INSECURE=' .env 2>/dev/null || echo "COMFYUI_INSECURE=1" >> .env
        echo "--- 已在 .env 写入 COMFYUI_INSECURE=1(跳过证书校验)"
    fi
fi

# 4) 本站端口
read -rp "本站监听端口 [默认 5012]: " port
PORT="${port:-5012}"
grep -q '^PORT=' .env 2>/dev/null || echo "PORT=$PORT" >> .env

# 5) 整理库目录:默认 <安装目录>/library
LIB_DIR="$ROOT/library"

# 6) 写入初始配置并测试连接
echo "--- 初始化数据库与配置"
venv/bin/python - <<PYEOF
import db
db.init_db()
db.set_setting("comfy_url", "$COMFY_URL")
db.set_setting("library_dir", "$LIB_DIR")
print("配置已写入: comfy_url=$COMFY_URL, library_dir=$LIB_DIR")
PYEOF

echo "--- 测试 ComfyUI 连接"
if venv/bin/python - <<'PYEOF2'
import time
from comfy_client import client
for attempt in range(3):  # ComfyUI 忙碌时可能瞬时 503,重试
    try:
        stats = client.system_stats()
        dev = (stats.get("devices") or [{}])[0]
        print("连接成功:", dev.get("name") or "未知设备",
              "| ComfyUI", (stats.get("system") or {}).get("comfyui_version", ""))
        raise SystemExit(0)
    except SystemExit:
        raise
    except Exception as e:
        if attempt == 2:
            print("连接失败:", e)
            raise SystemExit(1)
        time.sleep(2)
PYEOF2
then
    :
else
    echo "警告: 连接失败——地址可在启动后到「设置」页修改,安装继续。"
fi

# 7) 是否立即启动
read -rp "立即启动网页? [Y/n]: " startnow
echo
echo "=== 安装完成 ==="
echo "  网页地址: http://127.0.0.1:$PORT"
echo "  整理库:   $LIB_DIR"
echo "  手动启动: deploy/start.sh (前台) / deploy/start.sh --daemon (后台)"
if [ "$SCHEME" = "https" ] && grep -q '^COMFYUI_INSECURE=' .env; then
    echo "  提示: .env 中 COMFYUI_INSECURE=1(自签证书)"
fi
echo "  开机自启: 运行 deploy/systemd-gen.sh 生成 systemd 单元后按提示安装"
echo "  局域网访问: 参考 deploy/nginx-comfyweb.conf 配置反代"

if [[ ! "${startnow:-}" =~ ^[Nn] ]]; then
    exec deploy/start.sh --daemon
fi
