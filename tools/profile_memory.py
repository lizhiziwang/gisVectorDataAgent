"""当前进程内存采样（Windows），用来定位运行时高占用。

用法：
    .venv\\Scripts\\python.exe tools\\profile_memory.py
"""
from __future__ import annotations

import os
import sys
import time
from ctypes import (POINTER, Structure, byref, c_int, c_size_t, c_ulong,
                    c_void_p, sizeof, windll)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PySide6.QtWidgets import QApplication

BASE = r"D:\gis-agent\data\uploads\2415b1ef\x_zhejiang-lat"
FILES = ["gis_osm_landuse_a_free_1.shp", "gis_osm_buildings_a_free_1.shp",
         "gis_osm_water_a_free_1.shp", "gis_osm_roads_free_1.shp"]


class PROCESS_MEMORY_COUNTERS_EX(Structure):
    _fields_ = [
        ("cb", c_ulong),
        ("PageFaultCount", c_ulong),
        ("PeakWorkingSetSize", c_size_t),
        ("WorkingSetSize", c_size_t),
        ("QuotaPeakPagedPoolUsage", c_size_t),
        ("QuotaPagedPoolUsage", c_size_t),
        ("QuotaPeakNonPagedPoolUsage", c_size_t),
        ("QuotaNonPagedPoolUsage", c_size_t),
        ("PagefileUsage", c_size_t),
        ("PeakPagefileUsage", c_size_t),
        ("PrivateUsage", c_size_t),
    ]


_GET_MEM = None
try:
    _GET_MEM = windll.psapi.GetProcessMemoryInfo
except Exception:
    _GET_MEM = getattr(windll.kernel32, "K32GetProcessMemoryInfo", None)
if _GET_MEM is not None:
    _GET_MEM.argtypes = [c_void_p, POINTER(PROCESS_MEMORY_COUNTERS_EX), c_ulong]
    _GET_MEM.restype = c_int


def mem_mb() -> tuple[float, float, float]:
    c = PROCESS_MEMORY_COUNTERS_EX()
    c.cb = sizeof(c)
    h = windll.kernel32.GetCurrentProcess()
    if _GET_MEM is None or not _GET_MEM(h, byref(c), sizeof(c)):
        return (0.0, 0.0, 0.0)
    return (c.WorkingSetSize / 1048576.0, c.PrivateUsage / 1048576.0,
            c.PeakWorkingSetSize / 1048576.0)


def show(tag: str) -> None:
    ws, priv, peak = mem_mb()
    print(f"{tag:<28} WS={ws:8.1f}MB  Private={priv:8.1f}MB  "
          f"PeakWS={peak:8.1f}MB", flush=True)


def main() -> int:
    show("startup")
    app = QApplication.instance() or QApplication([])
    show("QApplication")

    from native.app import MainWindow

    win = MainWindow()
    win.resize(1440, 900)
    win.show()
    app.processEvents()
    show("MainWindow")

    paths = [os.path.join(BASE, f) for f in FILES
             if os.path.exists(os.path.join(BASE, f))]
    t0 = time.perf_counter()
    win.load_paths(paths)
    show(f"load_paths x{len(paths)}")

    deadline = time.perf_counter() + 90
    ready_since = None
    while time.perf_counter() < deadline:
        app.processEvents()
        mv = win.map
        ready = (not mv._has_pending()) and (not getattr(mv, "_vec_busy", False))
        if ready and len(mv.layers) >= len(paths) and mv._frame is not None:
            if ready_since is None:
                ready_since = time.perf_counter()
            if time.perf_counter() - ready_since > 1.0:
                break
        else:
            ready_since = None
        time.sleep(0.01)
    show("render settled")

    # 模拟一次缩放/全图，看缓存是否继续膨胀
    win.map.fit_all()
    for _ in range(40):
        app.processEvents()
        time.sleep(0.01)
    show("after fit_all")

    win.map.zoom_by(1.0)
    for _ in range(80):
        app.processEvents()
        time.sleep(0.01)
    show("after zoom_in")

    mv = win.map
    print(f"layers={len(mv.layers)} vec_cache={len(getattr(mv, '_cache', {}))} "
          f"img_cache={len(getattr(mv, '_img_cache', {}))} "
          f"layer_imgs={len(getattr(mv, '_layer_imgs', {}))} "
          f"raster_imgs={len(getattr(mv, '_raster_imgs', {}))}", flush=True)
    for lid, c in getattr(mv, "_cache", {}).items():
        sets = getattr(c, "sets", [])
        print(f"  cache {lid}: sets={len(sets)} stream={getattr(c,'stream',None)} "
              f"provider={getattr(c,'provider',None) is not None} "
              f"geoms={getattr(c,'geoms',None) is not None} "
              f"bounds={getattr(c,'bounds',None) is not None}", flush=True)
    show("before close")
    win.close()
    app.processEvents()
    show("after close")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
