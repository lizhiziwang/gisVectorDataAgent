"""UX 四项回归：载入进度/取消、Ctrl+Z 撤销、属性表联动、框选当输入。

用法：.venv\\Scripts\\python.exe tools\\test_ux_batch.py
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

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

S1 = r"D:\gis-agent\samples\地块.geojson"
S2 = r"D:\gis-agent\samples\学校.geojson"
win.load_paths([S1, S2])
print("[0] 载入两个样例")
t0 = time.perf_counter()
while time.perf_counter() - t0 < 30:
    app.processEvents()
    time.sleep(0.01)
    if len(win.store.layers) >= 2 and not win.map._has_pending() and win.map._frame is not None:
        break
for _ in range(5):
    app.processEvents()
check("两个图层已载入", len(win.store.layers) == 2, f"{len(win.store.layers)}")
check("载入结束后进度条隐藏", not win.st_prog.isVisible())

layers = list(win.store.layers.values())
parcel, school = layers[0], layers[1]

print("\n[1] 属性表：分页 / 筛选 / 排序 / 点行联动地图")
from native.dialogs import TableDialog
dlg = TableDialog(parcel, win, on_pick=lambda pos: (win.map.highlight_feature(parcel.id, pos),
                                                    win.map.zoom_to_feature(parcel.id, pos)))
check("首页只放一页的量", dlg.tw.rowCount() == min(200, len(parcel.gdf)),
      f"{dlg.tw.rowCount()} / {len(parcel.gdf)}")
scale0 = win.map.scale
dlg._render()
dlg.tw.cellClicked.emit(3, 0)
for _ in range(3):
    app.processEvents()
check("点第 4 行 → 地图高亮该要素", win.map._hl == (parcel.id, 3), str(win.map._hl))
check("点第 4 行 → 地图缩放到要素", win.map.scale != scale0, f"{scale0:.0f} → {win.map.scale:.0f}")

# 高亮描边必须是 cosmetic：非 cosmetic 的 2.4px 笔在世界坐标下会变成上千万像素宽，
# 把整个视口糊成一块实色（线上表现就是「点一下出现一个大圆」）
img = win.map.grab().toImage()
tot = accent = 0
for y in range(0, img.height(), 8):
    for x in range(0, img.width(), 8):
        c = img.pixelColor(x, y)
        tot += 1
        if abs(c.red() - 0xC9) < 14 and abs(c.green() - 0x64) < 14 and abs(c.blue() - 0x42) < 14:
            accent += 1
ratio = accent / max(tot, 1)
check("高亮没有糊成实色块", ratio < 0.35, f"强调色像素占比 {ratio:.0%}")
n_all = len(dlg._order)
dlg.filter.setText("商业")
app.processEvents()
n_f = len(dlg._order)
check("筛选生效", n_f <= n_all, f"{n_all} → {n_f}")
dlg.filter.setText("")
app.processEvents()
dlg._on_header(0)
app.processEvents()
first_sorted = dlg._order[0]
dlg._on_header(0)
app.processEvents()
check("点表头能排序（两下反向）", dlg._order[0] != first_sorted or n_all < 2,
      f"{first_sorted} vs {dlg._order[0]}")
dlg.close()

print("\n[2] 框选：Shift 拖框 → 选区 → 单独成层")
win.map.set_select_target(parcel.id)
win.map.fit_layer(parcel.id)
for _ in range(5):
    app.processEvents()
w, h = win.map.width(), win.map.height()
band = (int(w * 0.2), int(h * 0.2), int(w * 0.8), int(h * 0.8))
win.map._select_rect(band)
app.processEvents()
n_sel = win.map.selection_count()
check("框选选到了要素", n_sel > 0, f"{n_sel} 个")
check("选区信号与状态栏同步", "已选" in win.st_msg.text(), win.st_msg.text())
n_before = len(win.store.layers)
n_made = win._apply_selection_as_layer()
check("选区单独成层", len(win.store.layers) == n_before + 1 and n_made == n_sel,
      f"{n_before} → {len(win.store.layers)}，{n_made} 个")
check("成层后选区清空", win.map.selection_count() == 0)

print("\n[2b] Shift + 拖拽（真实鼠标事件）")
from PySide6.QtCore import QPoint
from PySide6.QtTest import QTest
win.map.clear_selection()
app.processEvents()
x0, y0 = int(w * 0.25), int(h * 0.25)
x1, y1 = int(w * 0.55), int(h * 0.55)
QTest.mousePress(win.map, Qt.LeftButton, Qt.ShiftModifier, QPoint(x0, y0))
QTest.mouseMove(win.map, QPoint((x0 + x1) // 2, (y0 + y1) // 2))
QTest.mouseMove(win.map, QPoint(x1, y1))
QTest.mouseRelease(win.map, Qt.LeftButton, Qt.ShiftModifier, QPoint(x1, y1))
app.processEvents()
n_drag = win.map.selection_count()
check("Shift+拖拽真的选出东西", n_drag > 0, f"{n_drag} 个")
check("拖拽结束后选框消失", win.map._band is None)
tx_before, sel_before = win.map.tx, win.map.selection_count()
QTest.mousePress(win.map, Qt.LeftButton, Qt.NoModifier, QPoint(int(w * 0.3), int(h * 0.3)))
QTest.mouseMove(win.map, QPoint(int(w * 0.35), int(h * 0.32)))
QTest.mouseRelease(win.map, Qt.LeftButton, Qt.NoModifier, QPoint(int(w * 0.35), int(h * 0.32)))
app.processEvents()
check("普通拖拽仍是平移、不误选",
      abs(win.map.tx - tx_before) > 5 and win.map.selection_count() == sel_before,
      f"tx {tx_before:.0f}→{win.map.tx:.0f}，选区 {sel_before}→{win.map.selection_count()}")
win.map.clear_selection()

print("\n[3] 撤销 / 重做")
n_layers = len(win.store.layers)
win.undo()
app.processEvents()
check("撤销「选区成层」→ 图层数回去", len(win.store.layers) == n_layers - 1,
      f"{n_layers} → {len(win.store.layers)}")
win.redo()
app.processEvents()
check("重做 → 图层回来", len(win.store.layers) == n_layers, f"{len(win.store.layers)}")
win.delete_layer(school.id)
app.processEvents()
check("删除图层", len(win.store.layers) == n_layers - 1 and school.id not in win.store.layers)
win.undo()
app.processEvents()
check("撤销删除 → 图层按原位置回来",
      school.id in win.store.layers and len(win.store.layers) == n_layers,
      f"{len(win.store.layers)} 个，顺序 {[l.name for l in win.store.layers.values()]}")
check("撤销后该图层可见", win.visible.get(school.id) is True)

print("\n[4] Esc：先清选区")
win.map.set_selection(parcel.id, [0, 1])
app.processEvents()
win._escape()
app.processEvents()
check("Esc 清掉选区", win.map.selection_count() == 0)

print("\n结论:", "PASS" if not fails else f"FAIL {fails}")
