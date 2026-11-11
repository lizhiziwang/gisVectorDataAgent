"""provider 模式回归：源文件图层按 bbox 读取，不把整层读进内存。

跑法：
    .venv\\Scripts\\python.exe tools\\test_provider_render.py

检查：
1. `Store.add_source()` 只读文件头，`_gdf` 仍为 None；
2. `MapView` 走 provider 流式缓存，`geoms` / `bounds` / `sets` 都为空；
3. 渲染结果与整份读入后走普通渲染集的画面基本一致；
4. 渲染后 `_gdf` 仍然是 None（证明渲染没有偷偷全量加载）。
"""
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import geopandas as gpd
import numpy as np
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from app.engine import Layer, Store
from native.mapview import MapView

DATA = Path(r"D:\gis-agent\data\uploads\2415b1ef\x_zhejiang-lat\gis_osm_water_a_free_1.shp")
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


def render(lay, on_progress=None):
    mv = MapView()
    mv.resize(960, 640)
    mv.set_layers([lay])
    mv.fit_all()
    if on_progress is not None:
        mv._worker.layerProgress.connect(on_progress)
    dt = settle(mv)
    return mv, dt, grab(mv)


def main() -> int:
    app = QApplication.instance() or QApplication([])
    if not DATA.exists():
        print("缺少测试数据，跳过")
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="gis-provider-"))
    store = Store(cache_dir=tmp)
    prov_lay = store.add_source(DATA, "water-provider", source="test", op="load")
    check("add_source 不读全量", prov_lay._gdf is None)
    check("provider 可用", prov_lay.provider is not None)
    meta = prov_lay.meta()
    check("文件头元信息", int(meta.get("count") or 0) == 30885, f"count={meta.get('count')}")
    check("文件头范围", bool(prov_lay.wgs_bounds), f"bounds={prov_lay.wgs_bounds}")

    progress = []
    mv1, dt1, a = render(prov_lay, lambda lid, img, epoch: progress.append(
        (lid, img.width(), img.height())))
    c1 = mv1._cache[prov_lay.id]
    print(f"  provider: {dt1*1000:.0f}ms, count={c1.total}")
    check("走 provider 流式", bool(c1.stream and c1.provider is not None))
    check("不建全量几何", c1.geoms is None)
    check("不建全量包围盒", c1.bounds is None)
    check("不建渲染集", len(c1.sets) == 0, f"sets={len(c1.sets)}")
    check("有中间帧（流式上屏）", bool(progress), f"n={len(progress)}")
    check("渲染后仍未全量加载", prov_lay._gdf is None)
    check("画面非空", int((a[:, :, 3] > 0).sum()) > a.shape[0] * a.shape[1] * 0.001)
    # 点击拾取不能为了属性把整层读进内存（大图层会到数 GB）
    mv1._hit_test(mv1.width() / 2, mv1.height() / 2)
    check("点击拾取后仍未全量加载", prov_lay._gdf is None)
    mv1.shutdown()

    full = Layer(id="L-full", name="water-full", gdf=gpd.read_file(DATA),
                 color=prov_lay.color)
    mv2, dt2, b = render(full)
    print(f"  full:     {dt2*1000:.0f}ms")
    mv2.shutdown()

    if a.shape == b.shape:
        diff = np.any(a != b, axis=2)
        ratio = int(diff.sum()) / diff.size
        print(f"  像素差异 {ratio*100:.3f}%")
        check("provider 与整份读取画面一致", ratio < 0.02, f"{ratio*100:.2f}%")
    else:
        check("画面尺寸一致", False, f"{a.shape} vs {b.shape}")

    store.clear(preserve_cache=False)
    print("\n" + ("FAIL: " + ", ".join(FAIL) if FAIL else "PASS"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
