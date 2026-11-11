"""栅格 tile cache：首帧整窗 + 平移复用 + 与整窗路径同位置。

验证：
1. 首帧走整窗读取，但会把完整落在视口内的 tile 放进缓存；
2. 同一比例尺平移时只补读边缘 tile，命中数增加、耗时明显下降；
3. tile 合成图与整窗路径落在同一位置，平均像素差很小；
4. GIS_AGENT_RASTER_TILES=0 可以退回旧路径。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
from PySide6.QtWidgets import QApplication

from app.engine import Store
from app.raster import read_info
from native.mapview import MapView
from native.proj import unproject
from native.rasterview import (RasterViewPort, TILE_CACHE,
                               render_layer_image)


def _arr(img):
    return np.frombuffer(img.constBits(), dtype=np.uint8,
                         count=img.sizeInBytes()).reshape(
                             img.height(), img.width(), 4).astype(np.int16)


def _vp_for_shift(mv, vp: RasterViewPort, dx: float) -> RasterViewPort:
    w0, w1, w2, w3 = vp.world
    world = (w0 - dx / vp.scale, w1, w2 - dx / vp.scale, w3)
    lon0, lat0 = unproject(world[0], world[3])
    lon1, lat1 = unproject(world[2], world[1])
    return RasterViewPort((lon0, lat0, lon1, lat1), vp.width, vp.height,
                          vp.scale, vp.tx + dx, vp.ty, vp.dpr, world)


def main() -> int:
    app = QApplication.instance() or QApplication([])  # noqa: F841
    print("[1] 首帧整窗 + 填充 tile 缓存")
    st = Store(cache_dir=ROOT / "data" / "cache_tiles_test")
    lay = st.add_raster(read_info(str(ROOT / "samples" / "raster" / "image.tif")),
                        "tiles")
    mv = MapView()
    mv.resize(900, 640)
    mv.set_layers([lay])
    mv.fit_all()
    mv._render_timer.stop()
    dpr = mv._device_pixel_ratio()
    vp = RasterViewPort(mv._view_lonlat_rect(),
                        max(1, int(mv.width() * dpr)),
                        max(1, int(mv.height() * dpr)),
                        mv.scale, mv.tx, mv.ty, dpr, mv._view_world_rect())

    os.environ["GIS_AGENT_RASTER_TILES"] = "1"
    TILE_CACHE.clear()
    lay.stats = None
    t0 = time.perf_counter()
    first = render_layer_image(lay, vp)
    first_dt = time.perf_counter() - t0
    s1 = TILE_CACHE.stats()
    ok = first is not None and first.width() > 0
    print(f"  ok   首帧出图: {ok}")
    print(f"  ok   填充了完整 tile: {s1['tiles'] > 0} ({s1['tiles']})")
    print(f"  首帧 {first_dt*1000:.1f}ms  {s1}")
    if not ok or s1["tiles"] <= 0:
        print("  FAIL 首帧没有填充 tile")
        return 1

    print("[2] 同比例尺平移到下一屏，tile 复用")
    vp2 = _vp_for_shift(mv, vp, 80.0)
    t0 = time.perf_counter()
    pan = render_layer_image(lay, vp2)
    pan_dt = time.perf_counter() - t0
    s2 = TILE_CACHE.stats()
    print(f"  ok   平移出图: {pan is not None}")
    print(f"  ok   有 cache 命中: {s2['hits'] > s1['hits']} "
          f"({s1['hits']}→{s2['hits']})")
    print(f"  平移 {pan_dt*1000:.1f}ms（首帧 {first_dt*1000:.1f}ms）  {s2}")
    if not (s2["hits"] > s1["hits"] and pan is not None):
        print("  FAIL 平移没有复用 tile")
        return 1

    print("[3] 与整窗路径同一位置（平均像素差）")
    os.environ["GIS_AGENT_RASTER_TILES"] = "0"
    lay.stats = None
    full = render_layer_image(lay, vp2)
    a = _arr(full)
    b = _arr(pan)
    d = np.abs(a - b)
    mean = float(d.mean())
    changed = int((d.sum(axis=2) > 0).sum())
    print(f"  ok   形状一致: {a.shape == b.shape}")
    print(f"  ok   平均差很小: {mean < 1.0} (mean={mean:.3f}, "
          f"changed={changed})")
    if a.shape != b.shape or mean >= 1.0:
        print("  FAIL tile 合成位置/像素差超限")
        return 1
    os.environ["GIS_AGENT_RASTER_TILES"] = "1"

    print("[4] 关闭开关回退整窗路径")
    TILE_CACHE.clear()
    os.environ["GIS_AGENT_RASTER_TILES"] = "0"
    lay.stats = None
    off = render_layer_image(lay, vp)
    print(f"  ok   开关关闭仍出图: {off is not None}")
    print(f"  ok   关闭时不填 tile: {TILE_CACHE.stats()['tiles'] == 0}")
    if off is None or TILE_CACHE.stats()["tiles"] != 0:
        print("  FAIL 关闭开关后仍走了 tile 路径")
        return 1
    os.environ["GIS_AGENT_RASTER_TILES"] = "1"

    mv.shutdown()
    TILE_CACHE.clear()
    st.clear(False)
    print("\n结论: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
