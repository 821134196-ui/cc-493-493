#!/usr/bin/env bash
# 一条命令启动消防通道临停管控系统（仅需 Python 3.10+，无第三方依赖）
set -e
cd "$(dirname "$0")"
PORT="${1:-8000}"
echo "启动中…… 浏览器打开 http://127.0.0.1:${PORT}"
exec python3 server.py --host 127.0.0.1 --port "${PORT}"
