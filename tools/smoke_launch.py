"""启动即退：验证桌面端真实启动路径（不开 offscreen）能起来并干净退出。

做法：跑 native_app.py 的 main()，但用一个定时器在 3 秒后关闭窗口。
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 不要 offscreen —— 这里就测真实启动路径
os.environ.pop("QT_QPA_PLATFORM", None)
os.environ["GIS_AGENT_PERSIST"] = "1"

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from native.app import MainWindow, make_icon

app = QApplication(sys.argv)
app.setApplicationName("GIS 矢量数据 Agent")
win = MainWindow()
win.resize(1280, 800)
win.show()

fails = []


def check(name, got, want=True):
    ok = got == want if not callable(want) else bool(want(got))
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        fails.append(name)


def probe():
    check("窗口已显示", win.isVisible(), True)
    check("任务中心已建", getattr(win, "task_center", None) is not None, True)
    check("记忆按钮已建", getattr(win, "btn_memory", None) is not None, True)
    check("持久化开启", win.persist, True)
    check("有会话", bool(win.session_id), True)
    check("地图正常", win.map is not None, True)
    app.quit()


QTimer.singleShot(2500, probe)
rc = app.exec()
check("退出码正常", rc, 0)
print("\n结论:", "PASS" if not fails else f"FAIL {fails}")
sys.exit(0 if not fails else 1)
