#!/usr/bin/env bash
# 一条命令启动:./run.sh  (自动使用 python3,首次运行写入演示数据)
# 可选: PORT=8080 ./run.sh | RESEED=1 ./run.sh
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8000}"
if [ "${RESEED:-}" != "" ]; then
  exec python3 -m app --port "$PORT" --reseed "$@"
else
  exec python3 -m app --port "$PORT" "$@"
fi
