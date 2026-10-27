#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

echo "Auto Cut Tool - 口播自动粗剪工具"
echo
echo "正在启动本地服务..."
echo "如果浏览器没有自动打开，请手动访问：http://127.0.0.1:8787"
echo

if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
else
  echo "未检测到 Python。请先安装 Python 3.10 或 3.11。"
  exit 1
fi

if command -v xdg-open >/dev/null 2>&1; then
  xdg-open "http://127.0.0.1:8787" >/dev/null 2>&1 || true
elif command -v open >/dev/null 2>&1; then
  open "http://127.0.0.1:8787" >/dev/null 2>&1 || true
fi

"$PYTHON_BIN" app/server.py
