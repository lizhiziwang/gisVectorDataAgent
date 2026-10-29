"""桌面窗口入口：在原生 Windows 窗口（WebView2）里打开 GIS 矢量数据 Agent。

不依赖任何浏览器标签页：
  · 后端 uvicorn 在后台线程里起在 127.0.0.1 的随机空闲端口（优先 8760）；
  · 窗口用 pywebview + Edge WebView2 渲染，关掉窗口即退出进程。
用法：
  python desktop.py       （带控制台，便于排查）
  pythonw desktop.py      （无控制台，启动.bat 用的就是这种）
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
import traceback
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

DATA = ROOT / "data"
DATA.mkdir(parents=True, exist_ok=True)
LOG_PATH = DATA / "app.log"

# pythonw 下没有控制台，stdout/stderr 是 None，任何写入都会抛异常 —— 统一重定向到日志
_logf = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
sys.stdout = _logf
sys.stderr = _logf


def log(msg: str) -> None:
    _logf.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


HOST = "127.0.0.1"


def pick_port(preferred: int = 8760) -> int:
    for p in [preferred] + list(range(preferred + 1, preferred + 20)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((HOST, p))
                return p
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return int(s.getsockname()[1])


SPLASH = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><style>
html,body{height:100%;margin:0;font-family:"Microsoft YaHei",system-ui,sans-serif;
background:linear-gradient(160deg,#f7f9fd,#e9eff9);color:#0f1b2d;display:grid;place-items:center}
.box{text-align:center}
.logo{width:56px;height:56px;margin:0 auto 16px;border-radius:15px;
background:linear-gradient(140deg,#2f6fed,#16408f);display:grid;place-items:center;
box-shadow:0 10px 26px -10px rgba(47,111,237,.65)}
h1{font-size:17px;margin:0 0 6px;font-weight:650;letter-spacing:.3px}
p{font-size:12.5px;color:#6b7c93;margin:0}
.bar{width:190px;height:3px;border-radius:2px;background:#dbe5f5;margin:18px auto 0;overflow:hidden}
.bar i{display:block;width:40%;height:100%;border-radius:2px;background:#2f6fed;
animation:run 1.1s ease-in-out infinite}
@keyframes run{0%{transform:translateX(-110%)}100%{transform:translateX(260%)}}
</style></head><body><div class="box">
<div class="logo"><svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="#fff"
stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7.5 9 4l6 3.5L21 4v12.5L15 20l-6-3.5L3 20z"/><path d="M9 4v12.5M15 7.5V20"/></svg></div>
<h1>GIS 矢量数据 Agent</h1><p>正在启动分析引擎…</p><div class="bar"><i></i></div>
</div></body></html>"""


def screen_metrics() -> tuple[int, int]:
    """取屏幕可用尺寸，让窗口开在合适大小并居中（取不到就用 1920x1040 兜底）。"""
    try:
        import ctypes
        u = ctypes.windll.user32
        sw, sh = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
        if sw > 400 and sh > 300:
            return sw, sh
    except Exception:
        pass
    return 1920, 1040


def window_geometry() -> dict:
    sw, sh = screen_metrics()
    w = min(1560, int(sw * 0.9))
    h = min(940, int(sh * 0.88))
    return {"width": w, "height": h, "x": max(0, (sw - w) // 2), "y": max(0, (sh - h) // 2)}


def error_html(detail: str) -> str:
    safe = detail.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><style>
body{{font-family:"Microsoft YaHei",system-ui,sans-serif;background:#fbfcfe;color:#0f1b2d;padding:40px}}
h2{{font-size:16px;color:#b42318}}pre{{background:#f4f6fa;border:1px solid #e3e8f0;border-radius:10px;
padding:14px;font-size:12px;white-space:pre-wrap;color:#3d4c63;max-height:60vh;overflow:auto}}
</style></head><body><h2>引擎启动失败</h2>
<p>请把下面信息发给维护者，或双击「调试启动.bat」查看控制台输出。</p>
<p>完整日志：<code>{LOG_PATH}</code></p><pre>{safe}</pre></body></html>"""


def main() -> None:
    port = pick_port(int(os.environ.get("GIS_AGENT_PORT", "8760")))
    url = f"http://{HOST}:{port}/"

    import uvicorn
    config = uvicorn.Config("app.main:app", host=HOST, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    log(f"server starting on {url}")

    try:
        import webview
    except Exception:
        log("pywebview 导入失败：\n" + traceback.format_exc())
        raise

    geo = window_geometry()
    window = webview.create_window(
        "GIS 矢量数据 Agent", html=SPLASH,
        width=geo["width"], height=geo["height"], x=geo["x"], y=geo["y"],
        min_size=(1080, 640),
        background_color="#eef2f8",
    )

    def load_when_ready() -> None:
        ok, last = False, ""
        for _ in range(240):  # 最多等 60s
            try:
                with urllib.request.urlopen(url + "api/health", timeout=1.5) as r:
                    if r.status == 200:
                        ok = True
                        break
            except Exception as e:  # noqa: BLE001
                last = repr(e)
            time.sleep(0.25)
        if ok:
            log("server ready, loading UI")
            try:
                window.load_url(url)
            except Exception:
                log("load_url 失败：\n" + traceback.format_exc())
                window.load_html(SPLASH)
        else:
            log(f"server not ready: {last}")
            window.load_html(error_html(f"服务未能在 60 秒内就绪：{last}\n日志：{LOG_PATH}"))

    threading.Thread(target=load_when_ready, daemon=True).start()

    try:
        webview.start()
    finally:
        server.should_exit = True
        log("window closed, shutting down")
        _logf.flush()
        os._exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        detail = traceback.format_exc()
        log("fatal:\n" + detail)
        try:  # 最兜底的提示框（连窗口都起不来时）
            import tkinter as tk
            from tkinter import messagebox
            r = tk.Tk()
            r.withdraw()
            messagebox.showerror("GIS 矢量数据 Agent", "启动失败：\n\n" + detail[-1200:])
        except Exception:
            pass
        sys.exit(1)
