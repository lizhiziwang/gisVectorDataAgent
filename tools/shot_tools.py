"""截左栏（图层 + 算子库分组）并打印分组几何，用来看分组排布是否合理。

离屏平台（offscreen）在这台机器上 QFontDatabase.families() 为 0，中文全是豆腐块，
所以这里用真 windows 平台 + WA_DontShowOnScreen：能拿到系统字体，又不会真的弹窗。
用法：.venv\\Scripts\\python.exe tools\\shot_tools.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from native.app import MainWindow

OUT = r"D:\gis-agent\docs\perf\tools-grouped.png"
SAMPLES = ["地块.geojson", "行政区.geojson", "道路.geojson", "学校.geojson"]

app = QApplication(sys.argv)
win = MainWindow()
win.setAttribute(Qt.WA_DontShowOnScreen, True)
win.resize(1440, 900)
win.show()
app.processEvents()

paths = [os.path.join(r"D:\gis-agent\samples", f) for f in SAMPLES]
paths = [p for p in paths if os.path.exists(p)]
win.load_paths(paths)

t0 = time.perf_counter()
while time.perf_counter() - t0 < 40:
    app.processEvents()
    time.sleep(0.01)
    if len(win.map.layers) >= len(paths) and not win.map._has_pending() and win.map._frame is not None:
        break
app.processEvents()

# ---- 分组几何打印 ----
gl = win.tool_grid
print("== 算子库网格 ==", f"行数 {gl.rowCount()} 列数 {gl.columnCount()}", flush=True)
for r in range(gl.rowCount()):
    cells = []
    for c in range(gl.columnCount()):
        it = gl.itemAtPosition(r, c)
        if it is None or it.widget() is None:
            cells.append("·")
            continue
        w = it.widget()
        txt = w.text() if isinstance(w, (QLabel, QPushButton)) else w.objectName()
        cells.append(f"{txt}({w.width()}x{w.height()})")
    print(f"  row{r:>2}: " + " | ".join(cells), flush=True)

print(f"\n算子数 badge = {win.tool_badge.text()}", flush=True)
host = win.tool_host
print(f"tool_host 需要高度 = {host.sizeHint().height()}", flush=True)
print(f"tool_scroll 可视高度 = {win.tool_scroll.viewport().height()}", flush=True)
print(f"layer_list 高度 = {win.layer_list.height()}", flush=True)

# ---- 截图 ----
panel = win.tool_scroll.parentWidget()
img = panel.grab()
img.save(OUT)
print(f"\n已截图 {OUT}  尺寸 {img.width()}x{img.height()}", flush=True)

img2 = win.tool_host.grab()
img2.save(r"D:\gis-agent\docs\perf\tools-host.png")
print(f"已截图 docs/perf/tools-host.png 尺寸 {img2.width()}x{img2.height()}", flush=True)

win.close()
app.processEvents()
print("关闭正常", flush=True)
