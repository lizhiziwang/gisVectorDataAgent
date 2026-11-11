"""并行图层 job 与串行结果逐像素一致。"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.engine import Store  # noqa: E402
from native.mapview import MapView  # noqa: E402
import native.mapview as mvmod  # noqa: E402

# 这个测试只比「并行画层」和「串行画层」逐像素是否一致。
# 宽视野整层粗路径缓存是**异步**建好的：两次渲染里缓存就绪的时机可能不同，
# 一帧用缓存、一帧走流式，像素当然不一样（缓存那帧反而更细）。
# 所以这里关掉 LOD 缓存构建，让两次渲染走同一条流式路径。
mvmod.LOD_MIN_FEATURES = 10 ** 9

BASE = ROOT / "data/uploads/2415b1ef/x_zhejiang-lat"
FILES = [
    ("water", BASE / "gis_osm_water_a_free_1.shp"),
    ("landuse", BASE / "gis_osm_landuse_a_free_1.shp"),
    ("buildings", BASE / "gis_osm_buildings_a_free_1.shp"),
]


def render(app: QApplication, parallel: bool):
    mvmod.PARALLEL_LAYERS = parallel
    store = Store(cache_dir=Path(tempfile.mkdtemp()))
    layers = [store.add_source(p, n, source="test", op="load")
              for n, p in FILES]
    mv = MapView()
    mv.resize(900, 640)
    mv.set_layers(layers)
    mv.fit_all()
    mv._render_timer.stop()
    progress = [0]

    def _p(*_a):
        progress[0] += 1

    mv._worker.layerProgress.connect(_p)
    t = time.perf_counter()
    mv._start_render()
    while time.perf_counter() - t < 120:
        app.processEvents()
        if mv._frame_sig == mv._scene_sig() and not mv._vec_busy:
            break
        time.sleep(0.005)
    img = mv._frame.toImage()
    arr = np.frombuffer(img.constBits(), dtype=np.uint8,
                        count=img.sizeInBytes()).reshape(
                            img.height(), img.width(), 4).copy()
    mv.shutdown()
    store.clear(False)
    return arr, time.perf_counter() - t, progress[0]


def main() -> int:
    if not all(p.is_file() for _, p in FILES):
        print("skip: sample missing")
        return 0
    app = QApplication.instance() or QApplication([])
    a, ta, pa = render(app, False)
    b, tb, pb = render(app, True)
    diff = int((np.abs(a.astype(np.int16) - b.astype(np.int16)).sum(axis=2) > 0).sum())
    print(f"串行 {ta:.2f}s progress={pa}；并行 {tb:.2f}s progress={pb}")
    print(f"像素差异 {diff}")
    ok = diff == 0 and pb > 0
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
