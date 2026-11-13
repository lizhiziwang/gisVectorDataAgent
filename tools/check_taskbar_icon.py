"""任务栏图标自检：真起一次应用，截屏看任务栏按钮上的图标。

用法：python tools/check_taskbar_icon.py [--wait 8] [--tag before]
输出：%TEMP%/taskbar_<tag>.png（整屏）与 %TEMP%/taskbar_<tag>_strip.png（任务栏那条）
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(r"D:\gis-agent")
PYW = ROOT / ".venv" / "Scripts" / "pythonw.exe"


def main() -> int:
    wait = 8.0
    tag = "taskbar"
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--wait" and i + 1 < len(args):
            wait = float(args[i + 1])
        elif a == "--tag" and i + 1 < len(args):
            tag = args[i + 1]

    env = dict(os.environ)
    env["GIS_AGENT_PERSIST"] = "0"
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("QT_QPA_PLATFORM", None)
    proc = subprocess.Popen([str(PYW), "native_app.py"], cwd=str(ROOT), env=env)
    print(f"已启动 pythonw pid={proc.pid}，等 {wait:.0f}s 让它画出来…")
    try:
        time.sleep(wait)
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        screen = app.primaryScreen()
        shot = screen.grabWindow(0)
        out = Path(os.environ.get("TEMP", r"C:\Users\zk\AppData\Local\Temp")) / f"{tag}.png"
        shot.save(str(out))
        print("整屏:", out, shot.width(), "x", shot.height())
        geo = screen.geometry()
        # 任务栏在屏幕最下面，截一条 60px 高的条（含按钮与其缩略图）
        strip = shot.copy(0, max(0, shot.height() - 60), shot.width(), 60)
        strip_out = out.with_name(out.stem + "_strip.png")
        strip.save(str(strip_out))
        print("任务栏条:", strip_out, "screen geo:", geo)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        print("已关闭应用")
    return 0


if __name__ == "__main__":
    sys.exit(main())
