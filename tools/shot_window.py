"""整窗截图，用于 README 首图 / 文档配图。

与 smoke_app.py 的区别只在渲染后端：smoke_app 用 offscreen（适合跑计时，但这台机器
离屏平台 QFontDatabase.families() 为 0，中文全是豆腐块）；这里用真 windows 平台 +
WA_DontShowOnScreen —— 能拿到系统字体，又不会真的把窗口弹到屏幕上。

用法：
    .venv\\Scripts\\python.exe tools\\shot_window.py                 # 存 preview.png
    .venv\\Scripts\\python.exe tools\\shot_window.py out.png 120     # 指定输出 / 等 120s
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from native.app import MainWindow

BASE = r"D:\gis-agent\data\uploads\2415b1ef\x_zhejiang-lat"
FILES = ["gis_osm_landuse_a_free_1.shp", "gis_osm_buildings_a_free_1.shp",
         "gis_osm_water_a_free_1.shp", "gis_osm_roads_free_1.shp"]

out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "preview.png")
budget = float(sys.argv[2]) if len(sys.argv) > 2 else 90

app = QApplication(sys.argv)
win = MainWindow()
win.setAttribute(Qt.WA_DontShowOnScreen, True)   # 能画、但不显示
win.resize(1440, 900)
win.show()
app.processEvents()

paths = [os.path.join(BASE, f) for f in FILES if os.path.exists(os.path.join(BASE, f))]
t0 = time.perf_counter()
win.load_paths(paths)
print(f"开始载入 {len(paths)} 个文件", flush=True)

while time.perf_counter() - t0 < budget:
    app.processEvents()
    time.sleep(0.01)
    mv = win.map
    if len(mv.layers) >= len(paths) and not mv._has_pending() and mv._frame is not None:
        break
app.processEvents()

img = win.grab()
img.save(out)
print(f"已截图 {out}  {img.width()}x{img.height()}  用时 {time.perf_counter()-t0:.1f}s", flush=True)
win.close()
app.processEvents()
