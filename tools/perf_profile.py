"""渲染耗时分解：把一帧的耗时拆成「构建 / 简化 / 绘制 / 合成」四块。

用法：.venv\\Scripts\\python.exe tools\\perf_profile.py
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication

from native import mapview
from native.app import MainWindow

BASE = r"D:\gis-agent\data\uploads\2415b1ef\x_zhejiang-lat"
FILES = ["gis_osm_landuse_a_free_1.shp", "gis_osm_buildings_a_free_1.shp",
         "gis_osm_water_a_free_1.shp", "gis_osm_roads_free_1.shp"]

acc: dict[str, dict[str, float]] = {}


def _slot(self, c):
    lid = next((k for k, v in self._cache.items() if v is c), "?")
    return acc.setdefault(lid, {})


def wrap(name, fn, layer_arg=1):
    def inner(*a, **kw):
        t = time.perf_counter()
        try:
            return fn(*a, **kw)
        finally:
            dt = (time.perf_counter() - t) * 1000
            obj = a[0]
            if hasattr(obj, "_cache") and len(a) > layer_arg:
                _slot(obj, a[layer_arg])[name] = _slot(obj, a[layer_arg]).get(name, 0) + dt
            else:
                acc.setdefault("__global__", {})[name] = \
                    acc.setdefault("__global__", {}).get(name, 0) + dt
    return inner


_orig_paint = mapview._paint_layer


def timed_paint(p, lay, view, scale, mpp=0.0):
    t = time.perf_counter()
    try:
        return _orig_paint(p, lay, view, scale, mpp)
    finally:
        d = acc.setdefault(lay.get("lid", "?"), {})
        d["paint"] = d.get("paint", 0) + (time.perf_counter() - t) * 1000


mapview._paint_layer = timed_paint
mapview.MapView._simplify_chunk = wrap("simplify", mapview.MapView._simplify_chunk)
mapview.MapView._build_job_chunk = wrap("build", mapview.MapView._build_job_chunk)
mapview.MapView._build_bounds_chunk = wrap("bounds", mapview.MapView._build_bounds_chunk)
mapview.MapView._commit_set = wrap("commit", mapview.MapView._commit_set)
mapview.MapView._compose = wrap("compose", mapview.MapView._compose)


def wait_frame(mv, timeout=180.0, since=None):
    """等一帧真正落到 _frame（epoch 变了且没有 pending 构建）。"""
    app = QApplication.instance()
    t0 = time.perf_counter()
    last = mv._frame_epoch if since is None else since
    while time.perf_counter() - t0 < timeout:
        app.processEvents()
        time.sleep(0.004)
        if mv._frame_epoch != last and not mv._has_pending():
            return time.perf_counter() - t0
    return time.perf_counter() - t0


def dump(win, title, wall):
    mv = win.map
    print(f"\n== {title} ==  端到端 {wall*1000:.0f} ms",
          f"| 视口 {mv.width()}x{mv.height()} @ {mv.mpp():.0f} m/px")
    tot = {}
    for lid, d in acc.items():
        name = "（全局）" if lid == "__global__" else next(
            (l.name for l in mv.layers if l.id == lid), lid)
        line = "  ".join(f"{k} {v:6.0f}" for k, v in d.items() if v > 0.5)
        if line:
            print(f"   {name[:34]:34s} {line}")
        for k, v in d.items():
            tot[k] = tot.get(k, 0) + v
    s = sum(tot.values())
    print("   合计 " + "  ".join(f"{k} {v:.0f}ms" for k, v in tot.items() if v > 0.5)
          + f"  | 未记账 {wall*1000 - s:.0f}ms")
    acc.clear()


def go(win, title, *, factor=None, dx=0):
    mv = win.map
    print(f"… {title}", flush=True)
    acc.clear()
    if factor:
        mv.zoom_at(factor)
    if dx:
        mv.tx += dx
        mv._invalidate_frame(0)
    mv._render_timer.stop()
    before = mv._frame_epoch
    mv._start_render()
    wall = wait_frame(mv, since=before)
    dump(win, title, wall)


app = QApplication(sys.argv)
win = MainWindow()
win.resize(1440, 900)
win.show()
paths = [os.path.join(BASE, f) for f in FILES if os.path.exists(os.path.join(BASE, f))]
acc.clear()
t0 = time.perf_counter()
win.load_paths(paths)
print(f"载入 {len(paths)} 个文件…", flush=True)
while len(win.map.layers) < len(paths) or win.map._frame is None or win.map._has_pending():
    app.processEvents()
    time.sleep(0.01)
print(f"载入+首帧 {(time.perf_counter()-t0):.1f}s，要素 "
      f"{sum(len(l.gdf) for l in win.map.layers):,}")
dump(win, "首次出图（含解析 + 建首套渲染集）", time.perf_counter() - t0)

go(win, "省域全景·同一渲染集（纯绘制）", factor=1.0001)
go(win, "缩到 ~1:1M（重建渲染集）", factor=win.map.mpp() / 92.0)
go(win, "缩到 ~30 m/px（重建渲染集）", factor=win.map.mpp() / 30.0)
go(win, "平移 120px（渲染集复用）", dx=-120)
go(win, "缩回省域（应命中缓存）", factor=win.map.mpp() / 950.0)

win.close()
app.processEvents()
