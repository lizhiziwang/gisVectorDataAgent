"""流式渲染回归：同一图层、同一视口，流式 vs 渲染集缓存，画面应基本一致。

跑法：
    .venv\\Scripts\\python.exe tools\\test_stream_render.py [name]

默认用 landuse（< 25 万要素，正常走渲染集缓存）；测试里强制
GIS_AGENT_STREAM_VECTOR=1，让同一图层改走屏幕范围流式渲染。
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from app.engine import Layer
from native.mapview import MapView

BASE = r"D:\gis-agent\data\uploads\2415b1ef\x_zhejiang-lat"
FILES = {
    "landuse": "gis_osm_landuse_a_free_1.shp",
    "water": "gis_osm_water_a_free_1.shp",
    "buildings": "gis_osm_buildings_a_free_1.shp",
    "roads": "gis_osm_roads_free_1.shp",
}
FAIL = []


def check(name, ok, extra=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"   {extra}"))
    if not ok:
        FAIL.append(name)


def settle(mv, max_wait=90.0):
    t0 = time.perf_counter()
    while mv._has_pending() and time.perf_counter() - t0 < max_wait:
        mv._build_tick()
        QApplication.processEvents()
        if time.perf_counter() - t0 > max_wait:
            break
    mv._start_render()
    want = (mv.scale, mv.tx, mv.ty, mv.width(), mv.height())
    while time.perf_counter() - t0 < max_wait:
        QApplication.processEvents()
        if mv._frame_view == want and mv._frame_sig == mv._scene_sig():
            return time.perf_counter() - t0
        time.sleep(0.005)
    return time.perf_counter() - t0


def grab(mv):
    src = mv._frame.toImage()
    img = QImage(src.width(), src.height(), QImage.Format_RGBA8888)
    img.fill(QColor("#f6f8fc"))
    p = QPainter(img)
    p.drawImage(0, 0, src)
    p.end()
    buf = np.frombuffer(img.constBits(), dtype=np.uint8, count=img.sizeInBytes())
    return buf.reshape(img.height(), img.bytesPerLine() // 4, 4)[:, :img.width(), :].copy()


def render(name, stream: bool):
    os.environ["GIS_AGENT_STREAM_VECTOR"] = "1" if stream else "0"
    import geopandas as gpd
    gdf = gpd.read_file(os.path.join(BASE, FILES[name]))
    lay = Layer(id="L0", name=name, gdf=gdf, color="#e8590c")
    mv = MapView()
    mv.resize(960, 640)
    mv.set_layers([lay])
    mv.fit_all()
    dt = settle(mv)
    c = mv._cache["L0"]
    print(f"  {'流式' if stream else '缓存'}: {len(gdf)} 要素, "
          f"sets={len(c.sets)}, {dt*1000:.0f}ms")
    return mv, c, grab(mv)


def main() -> int:
    app = QApplication.instance() or QApplication([])
    name = sys.argv[1] if len(sys.argv) > 1 else "landuse"
    if not os.path.exists(os.path.join(BASE, FILES[name])):
        print("缺测试数据，跳过")
        return 0

    mv1, c1, a = render(name, stream=True)
    mv1.shutdown()
    check("流式模式不建渲染集", len(c1.sets) == 0, f"sets={len(c1.sets)}")
    opaque = int((a[:, :, 3] > 0).sum())
    check("流式画面非空", opaque > a.shape[0] * a.shape[1] * 0.001, f"不透明像素 {opaque}")

    mv2, c2, b = render(name, stream=False)
    mv2.shutdown()
    check("缓存模式建了渲染集", len(c2.sets) > 0, f"sets={len(c2.sets)}")

    if a.shape == b.shape:
        diff = np.any(a != b, axis=2)
        n = int(diff.sum())
        ratio = n / diff.size
        print(f"  像素差异 {n}/{diff.size} = {ratio*100:.3f}%")
        # 两种管线的批次切分/简化时机略有不同，允许少量抗锯齿边缘差异；
        # 但绝不允许大片空洞。
        check("画面没有大面积差异", ratio < 0.02, f"{ratio*100:.2f}%")
    else:
        check("画面尺寸一致", False, f"{a.shape} vs {b.shape}")

    print("\n" + ("FAIL: " + ", ".join(FAIL) if FAIL else "PASS"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
