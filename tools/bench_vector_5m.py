"""500 万矢量点渲染基准（可生成合成数据）。

    # 生成 5M 点 GPKG（约 500 MB）并测全图/城市窗口
    python tools/bench_vector_5m.py --generate

    # 已有数据
    python tools/bench_vector_5m.py --gpkg path\\points.gpkg

指标：首个 provisional 帧延迟、整帧完成时间、progress 次数。
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def generate(path: Path, n: int = 5_000_000) -> None:
    import geopandas as gpd
    import numpy as np
    import pyogrio
    import shapely

    if path.exists():
        print(f"reuse {path}")
        return
    rng = np.random.default_rng(42)
    x = rng.uniform(118.0, 123.0, n)
    y = rng.uniform(27.0, 31.5, n)
    t = time.perf_counter()
    geoms = shapely.points(x, y)
    gdf = gpd.GeoDataFrame({"id": np.arange(n, dtype=np.int32)},
                           geometry=geoms, crs=4326)
    print(f"geometry {time.perf_counter() - t:.1f}s")
    t = time.perf_counter()
    pyogrio.write_dataframe(gdf, path, driver="GPKG", layer="points")
    print(f"write {time.perf_counter() - t:.1f}s, {path.stat().st_size / 1e6:.0f} MB")


def measure(mv, label: str) -> None:
    from PySide6.QtWidgets import QApplication

    first = [None]
    n = [0]

    def on_progress(*_a):
        n[0] += 1
        if first[0] is None:
            first[0] = time.perf_counter()

    mv._worker.layerProgress.connect(on_progress)
    t = time.perf_counter()
    mv._start_render()
    while time.perf_counter() - t < 180:
        QApplication.processEvents()
        if mv._frame_sig == mv._scene_sig() and not mv._vec_busy:
            break
        time.sleep(0.005)
    dt = time.perf_counter() - t
    print(f"{label}: first={((first[0] - t) if first[0] else -1) * 1000:.0f}ms "
          f"total={dt:.2f}s progress={n[0]}")
    mv._worker.layerProgress.disconnect(on_progress)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gpkg", default="")
    ap.add_argument("--generate", action="store_true")
    args = ap.parse_args()

    from PySide6.QtWidgets import QApplication
    from app.engine import Store
    from native.mapview import MapView

    if args.generate:
        path = Path(tempfile.gettempdir()) / "bench5m.gpkg"
        generate(path)
    elif args.gpkg:
        path = Path(args.gpkg)
    else:
        ap.error("需要 --gpkg 或 --generate")
    if not path.is_file():
        ap.error(f"文件不存在: {path}")

    app = QApplication.instance() or QApplication([])
    store = Store(cache_dir=Path(tempfile.mkdtemp()))
    lay = store.add_source(path, "points5m", source="bench", op="load")
    mv = MapView()
    mv.resize(1000, 700)
    mv.set_layers([lay])
    mv.fit_all()
    mv._render_timer.stop()
    measure(mv, "全图")
    mv.fit_bounds(120.1, 30.2, 120.3, 30.4)
    mv._render_timer.stop()
    measure(mv, "城市窗口")
    mv.shutdown()
    store.clear(False)
    if app is not None:
        app.processEvents()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
