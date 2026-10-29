"""步骤卡片三个按钮的回归：定位图层 / 属性表 / 导出。"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, r"D:\gis-agent")

from PySide6.QtWidgets import QApplication, QMenu, QPushButton

import native.app as A
from native.app import MainWindow
from native.chat import StepCard

LOG = open(r"D:\gis-agent\docs\perf\step-buttons.txt", "w", encoding="utf-8")


def p(*a):
    print(*a, flush=True)
    LOG.write(" ".join(str(x) for x in a) + "\n")
    LOG.flush()


app = QApplication(sys.argv)
win = MainWindow()
win.resize(1280, 800)
win.show()
win.load_samples()
t0 = time.time()
while time.time() - t0 < 30:
    app.processEvents()
    if win.map.layers and not win.map._has_pending():
        break
    time.sleep(0.01)

win.on_send("对地块做 500 米缓冲区")
t0 = time.time()
while time.time() - t0 < 60:
    app.processEvents()
    if not win._busy:
        break
    time.sleep(0.02)
for _ in range(20):
    app.processEvents()
    time.sleep(0.01)

cards = win.chat.msgs.findChildren(StepCard)
p("步骤卡片", len(cards))
btns = {b.text(): b for b in win.chat.msgs.findChildren(QPushButton)
        if b.text() in ("定位图层", "属性表", "导出")}
p("按钮数:", len(btns), "OK" if len(btns) == 3 else "缺按钮")

# 1) 定位图层：先缩得很远，再点它
win.map.zoom_at(0.15)
t0 = time.time()
while time.time() - t0 < 15:
    app.processEvents()
    if not win.map._has_pending():
        break
    time.sleep(0.01)
wide = round(win.map.scale)
btns["定位图层"].click()
t0 = time.time()
while time.time() - t0 < 15:
    app.processEvents()
    if not win.map._has_pending():
        break
    time.sleep(0.01)
after = round(win.map.scale)
p(f"定位图层: 缩远后 {wide} → {after}，{'OK' if after > wide * 2 else '没动'}")

# 2) 属性表：拦截模态 exec
seen = {}


def _fake_exec(self):
    seen["title"] = self.windowTitle()
    seen["cls"] = type(self).__name__
    return 0


A.TableDialog.exec = _fake_exec
btns["属性表"].click()
app.processEvents()
p("属性表:", seen, "OK" if seen.get("cls") == "TableDialog" else "没打开")

# 3) 导出：拦截菜单
menu_calls = {"n": 0}


class _FakeMenu:
    """替掉 QMenu：只为验证「导出」按钮确实弹了菜单，不真的进入模态循环。"""

    def __init__(self, *a, **k):
        menu_calls["n"] += 1
        self._items = []

    def addAction(self, label, *_a, **_k):
        self._items.append(label)

    def exec(self, *a, **k):
        menu_calls["items"] = list(self._items)
        return None


A.QMenu = _FakeMenu
btns["导出"].click()
app.processEvents()
p("导出菜单:", menu_calls, "OK" if menu_calls["n"] == 1 else "没弹出")

LOG.close()
