"""最小化的管线冒烟测试：单图层 + 逐阶段计时，输出实时刷新。"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, r"D:\gis-agent")

import geopandas as gpd
import numpy as np
from PySide6.QtWidgets import QApplication

from app.engine import Layer
from native.mapview import MapView

BASE = r"D:\gis-agent\data\uploads\2415b1ef\x_zhejiang-lat"
FILES = {"landuse": "gis_osm_landuse_a_free_1.shp",
         "buildings": "gis_osm_buildings_a_free_1.shp",
         "water": "gis_osm_water_a_free_1.shp"}

name = sys.argv[1] if len(sys.argv) > 1 else "landuse"
app = QApplication(sys.argv)
gdf = gpd.read_file(os.path.join(BASE, FILES[name]))
lay = Layer(id="L0", name=name, gdf=gdf, color="#e8590c")
mv = MapView()
mv.resize(1280, 800)
mv.set_layers([lay])
mv.fit_all()
c = mv._cache["L0"]
print(f"{name}: {len(gdf)} 要素 scale={mv.scale:.0f} mpp={mv.mpp():.0f}", flush=True)

t0 = time.perf_counter()
last = t0
while True:
    mv._build_tick()
    app.processEvents()
    now = time.perf_counter()
    if now - last > 1.0:
        j = c.build
        st = "—" if j is None else f"stage{j.stage} simpl {j.simplify_pos}/{len(j.idx)} paths {j.pos}/{len(j.simplify_out)} batched {len(j.spoly_paths)}"
        print(f"  {now-t0:5.1f}s  sets={len(c.sets)} {st}", flush=True)
        last = now
    if not mv._has_pending():
        break
    if time.perf_counter() - t0 > 90:
        print("!! 不收敛", flush=True)
        break
print(f"构建完成 {time.perf_counter()-t0:.2f}s  sets={len(c.sets)}", flush=True)
s = c.sets[-1]
print(f"  容差 {s.tol:.5f} rect={tuple(round(float(v),3) for v in s.rect)} "
      f"面批 {len(s.poly_paths)} 线批 {len(s.line_paths)} 点 {0 if s.dots is None else s.dots.size()}", flush=True)

t0 = time.perf_counter()
mv._start_render()
want = (mv.scale, mv.tx, mv.ty, mv.width(), mv.height())
while time.perf_counter() - t0 < 60:
    app.processEvents()
    if mv._frame_view == want and mv._frame_sig == mv._scene_sig():
        break
    time.sleep(0.005)
print(f"首帧 {time.perf_counter()-t0:.2f}s", flush=True)
if mv._frame is not None:
    from PySide6.QtGui import QColor, QImage, QPainter
    src = mv._frame.toImage()
    out = QImage(src.width(), src.height(), QImage.Format_RGB32)
    out.fill(QColor("#f6f8fc"))
    qp = QPainter(out)
    qp.drawImage(0, 0, src)
    qp.end()
    out.save(rf"D:\gis-agent\docs\perf\smoke-{name}.png")
    print("已存图", flush=True)
else:
    print("!! 没有帧", flush=True)
