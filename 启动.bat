@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
  echo [1/2] 首次运行，正在准备运行环境（约 3-5 分钟）...
  python -m venv .venv
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
  if errorlevel 1 (
    echo 依赖安装失败，请检查网络后重试。
    pause
    exit /b 1
  )
)

echo [2/2] 正在打开 GIS 矢量数据 Agent...
start "" ".venv\Scripts\pythonw.exe" "native_app.py"
exit
