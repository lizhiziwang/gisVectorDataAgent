"""道路绘制微基准（带像素校验）：同一套渲染集，换笔参数/画法，量真实耗时。

关键：必须用**真实平移量**把路径挪进画布，否则路径全部落在设备矩形外，
光栅引擎会立刻裁掉 —— 量出来的是「什么都没画」的时间（快 8 倍，纯属误导）。
每个变体都统计实际落笔像素数，用来确认这个变体真的画了东西。
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QApplication

from native.app import MainWindow
from native.mapview import _visible_indices

BASE = r"D:\gis-agent\data\uploads\2415b1ef\x_zhejiang-lat"
ROADS = os.path.join(BASE, "gis_osm_roads_free_1.shp")


def painted_pixels(img: QImage) -> int:
    """粗算落笔像素：按 4 行/列抽样统计非透明像素。"""
    w, h = img.width(), img.height()
    n = 0
    for y in range(0, h, 4):
        for x in range(0, w, 4):
            if img.pixelColor(x, y).alpha() > 8:
                n += 1
    return n * 16


def run(snap, idx, paths, *, width=1.6, cap=Qt.RoundCap, join=Qt.RoundJoin,
        aa=True, dot_px=None, bounds=None):
    img = QImage(int(snap["w"]), int(snap["h"]), QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, aa)
    p.translate(snap["tx"], snap["ty"])
    p.scale(snap["scale"], snap["scale"])
    p.setBrush(Qt.NoBrush)

    t = time.perf_counter()
    n_paths = n_dots = 0
    pen = QPen(QColor("#c96442"))
    if dot_px is None:
        pen.setWidthF(width)
        pen.setCapStyle(cap)
        pen.setJoinStyle(join)
        pen.setCosmetic(True)
        p.setPen(pen)
        for i in idx:
            p.drawPath(paths[i])
            n_paths += 1
    else:
        pen.setWidthF(width)
        pen.setCapStyle(cap)
        pen.setCosmetic(True)
        p.setPen(pen)
        pts = []
        lim = float(dot_px) / snap["scale"]        # 像素阈值换算成世界单位
        for i in idx:
            b = bounds[i]
            if max(b[2] - b[0], b[3] - b[1]) < lim:
                pts.append(QPointF((b[0] + b[2]) / 2, (b[1] + b[3]) / 2))
                n_dots += 1
            else:
                p.drawPath(paths[i])
                n_paths += 1
        if pts:
            hp = QPen(QColor("#c96442"))
            hp.setWidth(0)
            p.setPen(hp)
            p.drawPoints(QPolygonF(pts))
    dt = (time.perf_counter() - t) * 1000
    p.end()
    px = painted_pixels(img)
    del img
    return dt, n_paths, n_dots, px


app = QApplication(sys.argv)
win = MainWindow()
win.resize(1440, 900)
win.show()
win.load_paths([ROADS])
while len(win.map.layers) < 1 or win.map._frame is None or win.map._has_pending():
    app.processEvents()
    time.sleep(0.01)

mv = win.map
snap = mv._make_snapshot()
road = snap["layers"][0]
idx = [i for i in _visible_indices(road["line_bounds"], snap["view"], road["nline_b"])
       if i < road["nline"]]
paths = road["line_paths"]
verts = sum(paths[i].elementCount() for i in idx)
print(f"道路 {len(idx)} 批 / {verts:,} 顶点 @ {mv.mpp():.0f} m/px，视口 "
      f"{snap['w']}x{snap['h']}\n")

cases = [
    ("基线 1.6px RoundCap+Join AA", dict()),
    ("1.6px FlatCap+MiterJoin", dict(cap=Qt.FlatCap, join=Qt.MiterJoin)),
    ("1.2px RoundCap", dict(width=1.2)),
    ("1.0px RoundCap", dict(width=1.0)),
    ("hairline（width=0）", dict(width=0)),
    ("关抗锯齿（1.6px）", dict(aa=False)),
    ("关抗锯齿 + hairline", dict(width=0, aa=False)),
    ("短于 1.5px 的线段转点", dict(dot_px=1.5)),
    ("短于 3px 的线段转点", dict(dot_px=3)),
]

for name, kw in cases:
    best = None
    for _ in range(2):
        r = run(snap, idx, paths, bounds=road["line_bounds"], **kw)
        if best is None or r[0] < best[0]:
            best = r
    dt, np_, nd, px = best
    print(f"  {name:30s} {dt:7.0f} ms | 路径 {np_:5d} 点 {nd:5d} | 落笔约 {px:7,d} px")

win.close()
app.processEvents()
