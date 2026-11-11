"""宽视野渲染耗时对照：整层流式读 vs 整层粗路径缓存。

背景
----
shapefile / GPKG 没有空间索引时，视口盖住整个图层（省域、跨市）的每一次渲染
都要把文件顺序读一遍。47 万条道路的实测构成大约是：

    整层 provider 读     ~3.3s
    shapely 简化 + 建路径 ~2.5s
    ─────────────────────────
    一帧                  ~6.0s

拖动/缩放时这类帧要反复重来，就是用户感知的「卡住 5~8 秒」。

本脚本用真实 OSM 道路数据量这三段，并量「整层粗路径缓存」命中后的绘制耗时
（缓存按固定容差建一次，之后只在内存里按每批包围盒筛）。

用法：
    .venv\\Scripts\\python.exe tools\\perf_wide_view.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parent.parent
ROADS = ROOT / "data/uploads/2415b1ef/x_zhejiang-lat/gis_osm_roads_free_1.shp"


def _img(w=1400, h=900) -> QImage:
    img = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    return img


def main() -> int:
    if not ROADS.is_file():
        print("skip: 示例道路数据不在")
        return 0
    app = QApplication.instance() or QApplication(sys.argv)

    from app.engine import Store
    from native.mapview import (LOD_MAX_VERTS, LOD_MIN_MPP, LOD_TOL, MapView,
                                _paint_layer)
    from native.vectorstream import build_lod_paths, paint_stream_layer

    store = Store()
    layer = store.add_source(ROADS, "roads")
    prov = layer.provider
    wb = tuple(prov.wgs_bounds)
    pad = LOD_TOL * 2.0
    rect = (wb[0] - pad, wb[1] - pad, wb[2] + pad, wb[3] + pad)

    # 整省视图的 scale（和 fit_all 一致：范围铺满窗口）
    mv = MapView()
    mv.resize(1400, 900)
    mv.set_layers([layer])
    mv.fit_all()
    scale, tx, ty, _w, _h = mv.scale, mv.tx, mv.ty, mv.width(), mv.height()
    view = mv._view_world_rect()
    mpp = mv.mpp()
    snap_base = {
        "op": "", "color": "#c96442", "style": {}, "geom": "LineString",
        "stream": True, "provider": prov, "bbox": rect, "tol": 0.0,
        "point_dots": False,
    }

    print(f"数据：{prov.count} 条道路；整省视图 mpp={mpp:.0f}（缓存阈值 {LOD_MIN_MPP:.0f}）")

    # 1) 基线：按整层 bbox 流式读 + 建路径 + 画（= 改造前每次宽视野帧的开销）
    img = _img()
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.translate(tx, ty)
    p.scale(scale, scale)
    t0 = time.perf_counter()
    paint_stream_layer(p, {**snap_base, "tol": mv._simplify_tol()},
                       view, scale, mpp)
    p.end()
    t_stream = time.perf_counter() - t0

    # 2) 建一次整层粗路径缓存（后台线程干的就是这件事）
    t0 = time.perf_counter()
    payload = build_lod_paths(prov.iter_bbox(rect, batch_size=8192, columns=[]),
                              LOD_TOL, max_verts=LOD_MAX_VERTS)
    t_build = time.perf_counter() - t0

    # 3) 缓存命中：同一视图只用内存里的路径重画
    mv._on_lod_ready(layer.id, payload, rect, LOD_TOL, prov)
    for _ in range(200):
        app.processEvents()
        time.sleep(0.002)
        if mv._cache[layer.id].lod_state == "ready":
            break
    snap = mv._make_snapshot()
    entry = next(x for x in (snap or {}).get("layers", []) if x["lid"] == layer.id)
    assert not entry.get("stream"), "缓存就绪后快照应该直接用渲染集"
    img2 = _img()
    p = QPainter(img2)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.translate(tx, ty)
    p.scale(scale, scale)
    t0 = time.perf_counter()
    _paint_layer(p, entry, view, scale, mpp)
    p.end()
    t_cached = time.perf_counter() - t0

    nverts = payload[6]
    print(f"  流式整层帧（改造前）     {t_stream*1000:6.0f} ms")
    print(f"  建整层粗路径缓存（一次） {t_build*1000:6.0f} ms")
    print(f"  缓存命中帧（改造后）     {t_cached*1000:6.0f} ms"
          f"   （{nverts} 要素 / {len(payload[2])} 批路径）")
    if t_cached > 0:
        print(f"  宽视野帧提速             {t_stream/max(t_cached, 1e-6):5.1f}×")
    mv.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
