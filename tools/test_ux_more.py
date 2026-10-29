"""交互优化第二批回归：重命名 / 复制 / 只显示 / 键盘 / 中键 / 导出视图 / 输入历史。

用法：.venv\\Scripts\\python.exe tools\\test_ux_more.py
"""
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest

import native.app as app_mod
from native.app import MainWindow

fails = []


def check(name, cond, extra=""):
    print(("  OK  " if cond else "  !!  ") + name + (f": {extra}" if extra else ""))
    if not cond:
        fails.append(name)


app = QApplication(sys.argv)
win = MainWindow()
win.resize(1440, 900)
win.show()
app.processEvents()
win.load_paths([r"D:\gis-agent\samples\地块.geojson", r"D:\gis-agent\samples\道路.geojson"])
t0 = time.perf_counter()
while time.perf_counter() - t0 < 20:
    app.processEvents()
    time.sleep(0.01)
    if len(win.store.layers) >= 2 and win.map._frame is not None and not win.map._has_pending():
        break
for _ in range(5):
    app.processEvents()
parcel = list(win.store.layers.values())[0]
parcel_id, old_name = parcel.id, parcel.name

print("[1] 重命名（含撤销 / 重做、重名自动加序号）")
app_mod.QInputDialog.getText = staticmethod(lambda *a, **k: ("我的地块", True))
win.rename_layer(parcel_id)
app.processEvents()
check("名称已改", parcel.name == "我的地块", parcel.name)
win.undo()
app.processEvents()
check("撤销重命名", parcel.name == old_name, parcel.name)
win.redo()
app.processEvents()
check("重做重命名", parcel.name == "我的地块", parcel.name)
app_mod.QInputDialog.getText = staticmethod(lambda *a, **k: (list(win.store.layers.values())[1].name, True))
win.rename_layer(parcel_id)
app.processEvents()
check("重名自动加序号", parcel.name.endswith("(2)"), parcel.name)
win.undo()
app.processEvents()

print("\n[2] 复制图层")
n0 = len(win.store.layers)
win.duplicate_layer(parcel_id)
app.processEvents()
dup = list(win.store.layers.values())[-1]
check("多了同名副本", len(win.store.layers) == n0 + 1 and "副本" in dup.name, dup.name)
check("副本要素数一致", len(dup.gdf) == len(parcel.gdf))
win.undo()
app.processEvents()
check("撤销复制", len(win.store.layers) == n0, f"{len(win.store.layers)}")

print("\n[3] 只显示此层 / 显示全部")
win.show_only(parcel_id)
app.processEvents()
vis = [win.visible.get(l.id) for l in win.store.layers.values()]
check("只有一层可见", sum(1 for v in vis if v) == 1, str(vis))
row_ok = True
for i in range(win.layer_list.count()):
    item = win.layer_list.item(i)
    row = win.layer_list.itemWidget(item)
    if row is not None and row.eye.isChecked() != bool(win.visible.get(item.data(Qt.UserRole))):
        row_ok = False
check("复选框与状态一致", row_ok)
win.show_all()
app.processEvents()
check("显示全部", all(win.visible.get(l.id) for l in win.store.layers.values()))

print("\n[4] 键盘：方向键平移、+/- 缩放、中键拖拽")
win.map.setFocus()
tx0, sc0 = win.map.tx, win.map.scale
QTest.keyClick(win.map, Qt.Key_Right)
app.processEvents()
check("→ 平移", abs(win.map.tx - tx0) > 5, f"{tx0:.0f} → {win.map.tx:.0f}")
QTest.keyClick(win.map, Qt.Key_Plus)
app.processEvents()
check("+ 放大", win.map.scale > sc0 * 1.1, f"{sc0:.0f} → {win.map.scale:.0f}")
w, h = win.map.width(), win.map.height()
tx1 = win.map.tx
QTest.mousePress(win.map, Qt.MiddleButton, Qt.NoModifier, QPoint(w // 2, h // 2))
QTest.mouseMove(win.map, QPoint(w // 2 + 40, h // 2 + 10))
QTest.mouseRelease(win.map, Qt.MiddleButton, Qt.NoModifier, QPoint(w // 2 + 40, h // 2 + 10))
app.processEvents()
check("中键拖拽平移", abs(win.map.tx - tx1) > 20, f"{tx1:.0f} → {win.map.tx:.0f}")

print("\n[4b] 地图右键菜单")
win.map.set_selection(parcel_id, [0, 1, 2])
app.processEvents()
menu = win._build_map_menu()
texts = [a.text() for a in menu.actions()]
check("菜单含全图/导出视图", any("全图" in t for t in texts) and any("导出当前视图" in t for t in texts),
      " / ".join(t for t in texts if t))
check("有选区时出现「存成图层」", any("存成图层" in t for t in texts))
win.map.clear_selection()

print("\n[5] 导出当前视图 PNG")
out = os.path.join(tempfile.gettempdir(), "gis_view_test.png")
app_mod.QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (out, "PNG"))
win.export_view()
app.processEvents()
ok = os.path.exists(out) and os.path.getsize(out) > 20000
check("PNG 已生成且有内容", ok, f"{os.path.getsize(out) if os.path.exists(out) else 0} 字节")
if os.path.exists(out):
    os.remove(out)

print("\n[6] 输入框 ↑/↓ 翻历史")
from PySide6.QtCore import QEvent
from PySide6.QtGui import QKeyEvent
win.chat._hist.clear()
win.chat._hist_pos = -1
for t in ("第一条", "第二条"):
    win.chat.input.setPlainText(t)
    win.chat._emit_send()
app.processEvents()
up = lambda: QApplication.sendEvent(
    win.chat.input, QKeyEvent(QEvent.KeyPress, Qt.Key_Up, Qt.NoModifier))
down = lambda: QApplication.sendEvent(
    win.chat.input, QKeyEvent(QEvent.KeyPress, Qt.Key_Down, Qt.NoModifier))
up()
app.processEvents()
check("↑ 取回最后一条", win.chat.input.toPlainText() == "第二条", win.chat.input.toPlainText())
up()
app.processEvents()
check("再 ↑ 取回上一条", win.chat.input.toPlainText() == "第一条", win.chat.input.toPlainText())
down()
app.processEvents()
check("↓ 回到下一条", win.chat.input.toPlainText() == "第二条", win.chat.input.toPlainText())
down()
app.processEvents()
check("↓ 到底清空输入框", win.chat.input.toPlainText() == "", win.chat.input.toPlainText())

print("\n结论:", "PASS" if not fails else f"FAIL {fails}")
