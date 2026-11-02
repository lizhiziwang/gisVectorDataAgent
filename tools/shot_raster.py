"""截「栅格 + 矢量混合渲染」的整窗图，看实际观感。

offscreen 平台在这台机器上中文字体是豆腐块，所以用真 windows 平台 +
WA_DontShowOnScreen：能拿到系统字体，又不会真的弹窗。
用法：python tools/shot_raster.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 注意：这里**不**设 QT_QPA_PLATFORM=offscreen。
# 离屏平台在这台机器上 QFontDatabase.families() 为 0，中文全是豆腐块。
# 用真实的 windows 平台 + WA_DontShowOnScreen：能拿到系统字体，又不会真弹窗。
# Qt 还必须先于 rasterio 导入（否则 Windows 上 PySide6 导入会 access violation）。
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "perf", "raster-mixed-window.png")

app = QApplication(sys.argv)
from native.app import MainWindow

win = MainWindow()
win.setAttribute(Qt.WA_DontShowOnScreen, True)
win.resize(1440, 900)
win.show()
app.processEvents()


def pump(ms):
    t0 = time.time()
    while (time.time() - t0) * 1000 < ms:
        app.processEvents()
        time.sleep(0.01)


# 先矢量，再栅格（模拟拖入顺序）
vec = [os.path.join(ROOT, "samples", f)
       for f in ("地块.geojson", "行政区.geojson", "道路.geojson")]
vec = [p for p in vec if os.path.exists(p)]
win.load_paths(vec)
pump(2500)

ras = [os.path.join(ROOT, "samples", "raster", f) for f in ("dem.tif", "image.tif")]
ras = [p for p in ras if os.path.exists(p)]
win.load_paths(ras)
pump(2500)

win.map.fit_all()
pump(2500)

win.grab().save(OUT)
print("已保存:", OUT, os.path.getsize(OUT), "字节")
