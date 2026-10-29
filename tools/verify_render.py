"""渲染管线验证：真实数据 + 离屏渲染，检查「有没有地方没画出来」。

做法：
1. 用真实 shapefile 建 MapView（离屏），跑一串视图（省域 → 逐级放大 → 平移 → 缩回）；
2. 每个视图等构建/渲染结束后存一张 PNG；
3. 最后在同一个视口下做一次「清空重来」的全新构建，和缓存版的画面逐像素比对 ——
   如果渲染集的范围/要素有偷工减料（抽样、覆盖范围记账错误），
   两次画面就会不一样，差异区域正好就是屏幕上那些「没渲染出来的块」。

用法：python tools/verify_render.py [图层名...]
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from PySide6.QtWidgets import QApplication

from app.engine import Layer
from native.mapview import MapView

BASE = r"D:\gis-agent\data\uploads\2415b1ef\x_zhejiang-lat"
FILES = {
    "landuse": "gis_osm_landuse_a_free_1.shp",
    "buildings": "gis_osm_buildings_a_free_1.shp",
    "water": "gis_osm_water_a_free_1.shp",
    "roads": "gis_osm_roads_free_1.shp",
    "places": "gis_osm_places_free_1.shp",
}
COLORS = {
    "landuse": "#e8590c", "buildings": "#f08c00", "water": "#1f6feb",
    "roads": "#495057", "places": "#c2255c",
}

app = QApplication(sys.argv)

names = sys.argv[1:] or ["landuse", "buildings", "water"]
layers = []
for i, name in enumerate(names):
    import geopandas as gpd
    fn = FILES.get(name)
    if not fn:
        continue
    t0 = time.perf_counter()
    try:
        gdf = gpd.read_file(os.path.join(BASE, fn))
    except Exception as e:
        print(f"跳过 {name}: {e}")
        continue
    lay = Layer(id=f"L{i}", name=name, gdf=gdf, color=COLORS.get(name, "#2f6fed"))
    layers.append(lay)
    print(f"载入 {name}: {len(gdf)} 要素 {time.perf_counter()-t0:.1f}s")

mv = MapView()
mv.resize(1280, 800)
mv.set_layers(layers)


def settle(max_wait=90.0):
    """等构建 + 渲染都结束。"""
    t0 = time.perf_counter()
    ticks = 0
    while mv._has_pending() and time.perf_counter() - t0 < max_wait:
        mv._build_tick()
        app.processEvents()
        ticks += 1
        if ticks > 5000:
            print("!! 构建不收敛，放弃（说明覆盖判断有问题）")
            break
    mv._start_render()
    want = (mv.scale, mv.tx, mv.ty, mv.width(), mv.height())
    t1 = time.perf_counter()
    while time.perf_counter() - t1 < 30:
        app.processEvents()
        if mv._frame_view == want and mv._frame_sig == mv._scene_sig():
            break
        time.sleep(0.005)
    return time.perf_counter() - t0


def grab(path):
    """把透明底合成到应用底色上再存图（否则透明区域会被看成黑色空洞）。"""
    from PySide6.QtGui import QColor, QImage, QPainter
    src = mv._frame.toImage()
    img = QImage(src.width(), src.height(), QImage.Format_RGB32)
    img.fill(QColor("#f6f8fc"))
    p = QPainter(img)
    p.drawImage(0, 0, src)
    p.end()
    img.save(path)
    w, h = img.width(), img.height()
    buf = np.frombuffer(img.constBits(), count=img.sizeInBytes(), dtype=np.uint8)
    return buf.reshape(h, img.bytesPerLine() // 4, 4)[:, :w, :].copy()


def report(tag):
    print(f"\n=== {tag}  scale={mv.scale:.1f}  mpp={mv.mpp():.1f}")
    for lay in layers:
        c = mv._cache[lay.id]
        s = mv._pick_set(c)
        print(f"  {lay.name}: 要素 {c.total}, 集合 {len(c.sets)} 套, "
              f"当前容差 {s.tol if s else -1:.5f}°, "
              f"rect={'—' if s is None else tuple(round(float(v), 3) for v in s.rect)}, "
              f"路径 {len(s.poly_paths) if s else 0}+{len(s.line_paths) if s else 0}, "
              f"点 {0 if not s else (len(s.dot_xy) if s.dot_xy is not None else (s.dots.size() if s.dots else 0))}")
        if s is None:
            continue
        # 完备性校验：渲染集里应当包含 rect 范围内**所有**相交要素
        # （任何抽样/限流都会让这两个数字对不上 —— 那就是屏幕上的空洞）
        want = len(mv._select_indices(c, s.rect))
        got = s.nfeat
        flag = "OK" if got == want else f"!! 少了 {want - got} 个要素"
        print(f"    完备性: rect 内 {want} 个要素 / 渲染集 {got} 个 → {flag}")


shots = []
# 视图序列：省域 → 放大 4 级 → 向东平移 → 缩回
views = [("L0 省域", None), ("L1", 3.0), ("L2", 3.0), ("L3", 3.0)]
for tag, z in views:
    if z is None:
        mv.fit_all()
    else:
        mv.zoom_at(z, mv.width() * 0.5, mv.height() * 0.45)
    dt = settle()
    report(tag)
    print(f"  用时 {dt*1000:.0f}ms")
    shots.append((tag, grab(f"docs/perf/verify-{tag.split()[0]}.png")))

# 平移（拖动 40% 视口宽）
for k in range(3):
    mv.tx -= mv.width() * 0.4
    mv._invalidate_frame(0)
    dt = settle()
    print(f"平移 {k+1}: 用时 {dt*1000:.0f}ms")

mv.fit_all()
settle()
report("回到省域")
shot_cached = grab("docs/perf/verify-back.png")
shots.append(("back", shot_cached))

# ---- 关键校验：清空所有渲染集，重新构建同一视口，画面必须一致 ----
print("\n清空渲染集后重建同一视口…")
for lay in layers:
    c = mv._cache[lay.id]
    c.sets.clear()
    c.build = None
t0 = time.perf_counter()
settle()
print(f"重建用时 {(time.perf_counter()-t0)*1000:.0f}ms")
shot_fresh = grab("docs/perf/verify-fresh.png")

diff = np.any(shot_cached != shot_fresh, axis=2)
n = int(diff.sum())
total = diff.size
print(f"\n像素比对：不一致 {n} / {total} = {n/total*100:.4f}%")
if n:
    ys, xs = np.nonzero(diff)
    print(f"  差异区域 x[{xs.min()},{xs.max()}] y[{ys.min()},{ys.max()}]")
    # 把差异标红存一张，便于肉眼确认
    vis = shot_cached.copy()
    vis[diff] = [255, 0, 0, 255]
    from PySide6.QtGui import QImage
    h, w = diff.shape
    out = QImage(vis.tobytes(), w, h, w * 4, QImage.Format_RGB32)
    out.save("docs/perf/verify-diff.png")
    print("  差异标注 → docs/perf/verify-diff.png")
