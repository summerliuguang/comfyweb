#!/usr/bin/env bash
# ComfyWeb 一键启动脚本(在仓库根目录执行 deploy/start.sh)
#   deploy/start.sh            前台启动(Ctrl+C 停止,日志即终端输出)
#   deploy/start.sh --daemon   后台启动(日志写 logs/,PID 写 run/)
#   deploy/start.sh --stop     停止后台实例
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# 端口优先级:环境变量 > .env 的 PORT > 默认 5012
PORT="${PORT:-$(grep -E '^PORT=' "$ROOT/.env" 2>/dev/null | tail -1 | cut -d= -f2)}"
PORT="${PORT:-5012}"
PY="$ROOT/venv/bin/python"
GUNICORN="$ROOT/venv/bin/gunicorn"
mkdir -p "$ROOT/logs" "$ROOT/run"

if [ ! -x "$GUNICORN" ]; then
    echo "未找到虚拟环境,请先运行 deploy/install.sh" >&2
    exit 1
fi

GUNICORN_ARGS=(--chdir "$ROOT" --bind "127.0.0.1:$PORT"
    --workers 1 --threads 8 --worker-class gthread
    --timeout 360 --graceful-timeout 30 app:app)

case "${1:-}" in
    --stop)
        if [ -f "$ROOT/run/comfyweb.pid" ]; then
            PID="$(cat "$ROOT/run/comfyweb.pid")"
            kill "$PID" 2>/dev/null || echo "进程不存在"
            # 优雅关闭最长 graceful-timeout(30s),等它真正退出
            for _ in $(seq 1 35); do
                kill -0 "$PID" 2>/dev/null || break
                sleep 1
            done
            kill -0 "$PID" 2>/dev/null && { kill -9 "$PID" 2>/dev/null; echo "已强制结束"; }
            rm -f "$ROOT/run/comfyweb.pid"
            echo "已停止"
        else
            echo "没有后台实例(无 PID 文件)"
        fi
        ;;
    --daemon)
        if [ -f "$ROOT/run/comfyweb.pid" ] && kill -0 "$(cat "$ROOT/run/comfyweb.pid")" 2>/dev/null; then
            echo "已在后台运行(PID $(cat "$ROOT/run/comfyweb.pid"))"
            exit 0
        fi
        nohup "$GUNICORN" "${GUNICORN_ARGS[@]}" >> "$ROOT/logs/gunicorn.log" 2>&1 &
        PID=$!
        echo "$PID" > "$ROOT/run/comfyweb.pid"
        ok=""
        for _ in $(seq 1 10); do
            kill -0 "$PID" 2>/dev/null || break
            if curl -sf -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/healthz"; then
                ok=1
                break
            fi
            sleep 1
        done
        if [ -n "$ok" ]; then
            echo "已后台启动(PID $PID)"
            echo "网页地址: http://127.0.0.1:$PORT"
            echo "日志: $ROOT/logs/gunicorn.log   停止: deploy/start.sh --stop"
        else
            kill -0 "$PID" 2>/dev/null && kill "$PID" 2>/dev/null
            echo "启动失败(端口被占用或启动超时),查看日志: $ROOT/logs/gunicorn.log" >&2
            rm -f "$ROOT/run/comfyweb.pid"
            exit 1
        fi
        ;;
    "")
        echo "ComfyWeb 启动于 http://127.0.0.1:$PORT  (Ctrl+C 停止)"
        exec "$GUNICORN" "${GUNICORN_ARGS[@]}"
        ;;
    *)
        echo "用法: deploy/start.sh [--daemon|--stop]" >&2
        exit 1
        ;;
esac
