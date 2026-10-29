"""整机冒烟：真正启动 MainWindow、载入数据、等渲染稳定、截图。

验证的是「用户点开程序看到的东西」，而不是只看 MapView。
用法：python tools/smoke_app.py
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication

from native.app import MainWindow

BASE = r"D:\gis-agent\data\uploads\2415b1ef\x_zhejiang-lat"
FILES = ["gis_osm_landuse_a_free_1.shp", "gis_osm_buildings_a_free_1.shp",
         "gis_osm_water_a_free_1.shp", "gis_osm_roads_free_1.shp"]

app = QApplication(sys.argv)
win = MainWindow()
win.resize(1440, 900)
win.show()

paths = [os.path.join(BASE, f) for f in FILES if os.path.exists(os.path.join(BASE, f))]
t0 = time.perf_counter()
win.load_paths(paths)
print("开始载入", len(paths), "个文件", flush=True)

deadline = time.perf_counter() + 90
last = time.perf_counter()
while time.perf_counter() < deadline:
    app.processEvents()
    now = time.perf_counter()
    if now - last > 2:
        last = now
        mv = win.map
        ready = not mv._has_pending()
        print(f"  {now-t0:5.1f}s 图层 {len(mv.layers)} 构建完成={ready} "
              f"mpp={mv.mpp():.0f} 状态={win.st_message.text() if hasattr(win,'st_message') else ''}",
              flush=True)
        if ready and len(mv.layers) >= len(paths) and mv._frame is not None:
            break
    time.sleep(0.01)

print(f"总用时 {time.perf_counter()-t0:.1f}s", flush=True)
win.grab().save(r"D:\gis-agent\docs\perf\app-smoke.png")
print("已截图 docs/perf/app-smoke.png", flush=True)
win.close()
app.processEvents()
print("关闭正常", flush=True)
