"""服务模式入口：只起后端 HTTP 服务（不弹窗口、不开浏览器）。

普通使用请双击「启动.bat」，那会打开原生窗口（desktop.py）。
本文件用于：调试接口、被其它程序调用、或想自己用浏览器访问时。
    python run.py            # 仅服务
    python run.py --browser  # 仅服务 + 顺手打开系统浏览器
"""
from __future__ import annotations

import os
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

PORT = int(os.environ.get("GIS_AGENT_PORT", "8760"))
HOST = os.environ.get("GIS_AGENT_HOST", "127.0.0.1")

if __name__ == "__main__":
    if "--browser" in sys.argv:
        threading.Timer(1.5, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()
    print(f"\n  GIS 矢量数据 Agent · 服务模式  →  http://{HOST}:{PORT}\n")
    uvicorn.run("app.main:app", host=HOST, port=PORT, log_level="info")
