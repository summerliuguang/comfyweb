#!/usr/bin/env bash
# 生成 ComfyWeb 的 systemd 用户单元(含每日备份 timer),输出到当前目录。
# 安装动作由用户自行执行,脚本只负责按实际路径渲染模板:
#   sudo cp comfyweb.service /etc/systemd/system/
#   sudo systemctl daemon-reload && sudo systemctl enable --now comfyweb
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY="$ROOT/deploy"
USER_NAME="$(id -un)"
PORT="${PORT:-5012}"

read -rp "运行用户 [默认 $USER_NAME]: " u
USER_NAME="${u:-$USER_NAME}"
read -rp "监听端口 [默认 $PORT]: " p
PORT="${p:-$PORT}"

venv_python="$ROOT/venv/bin/python"
if [ ! -x "$ROOT/venv/bin/gunicorn" ]; then
    echo "错误: 未找到虚拟环境,请先运行 deploy/install.sh" >&2
    exit 1
fi

sed -e "s|User=youruser|User=$USER_NAME|" \
    -e "s|WorkingDirectory=/opt/comfyweb|WorkingDirectory=$ROOT|" \
    -e "s|--chdir /opt/comfyweb|--chdir $ROOT|" \
    -e "s|/opt/comfyweb/venv/bin/gunicorn|$ROOT/venv/bin/gunicorn|" \
    -e "s|--bind 127.0.0.1:5012|--bind 127.0.0.1:$PORT|" \
    "$DEPLOY/comfyweb.service" > "$ROOT/comfyweb.service.generated"
sed -e "s|User=youruser|User=$USER_NAME|" \
    -e "s|/opt/comfyweb|$ROOT|g" \
    "$DEPLOY/comfyweb-backup.service" > "$ROOT/comfyweb-backup.service.generated"
cp "$DEPLOY/comfyweb-backup.timer" "$ROOT/comfyweb-backup.timer.generated"

cat <<EOF

已生成(当前目录):
  comfyweb.service.generated         主服务(gunicorn,已按 $USER_NAME / $ROOT / 端口 $PORT 渲染)
  comfyweb-backup.service.generated  每日备份服务
  comfyweb-backup.timer.generated    备份定时器(03:30)

安装命令(自行执行):
  sudo cp comfyweb.service.generated /etc/systemd/system/comfyweb.service
  sudo cp comfyweb-backup.service.generated /etc/systemd/system/comfyweb-backup.service
  sudo cp comfyweb-backup.timer.generated /etc/systemd/system/comfyweb-backup.timer
  sudo systemctl daemon-reload
  sudo systemctl enable --now comfyweb comfyweb-backup.timer

注意: 若之前用 deploy/start.sh --daemon 启动过,先 deploy/start.sh --stop 再启用 systemd。
EOF
