@echo off
setlocal
cd /d "%~dp0"

echo Auto Cut Tool - 口播自动粗剪工具
echo.
echo 正在启动本地服务...
echo 如果浏览器没有自动打开，请手动访问：http://127.0.0.1:8787
echo.

python --version >nul 2>&1
if errorlevel 1 (
  echo 未检测到 python 命令。请先安装 Python 3.10 或 3.11，并加入 PATH。
  pause
  exit /b 1
)

start "" "http://127.0.0.1:8787"
python app\server.py

echo.
echo 服务已停止。
pause
