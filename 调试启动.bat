@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 带控制台的调试启动（出错时能直接看到输出）
echo.
".venv\Scripts\python.exe" "native_app.py"
pause
