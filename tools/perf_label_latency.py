"""标注延迟对照：按视口每次重读 vs 整层锚点表。

背景
----
没有空间索引的源文件（GeoParquet、无 .qix 的 Shapefile）按 bbox 取标注也要把
整个文件解一遍。614MB 的村界面 parquet 实测：

    按视口取一次标注      ~4s（拖动一次换一个视野就重来）
    整层锚点表（一次）    ~4s，2 万锚点约 2MB
    表建好后视口筛选      ~0.1ms（numpy 掩码 + 16×16 网格取样）

旧实现里 `_LabelWorker` 不能中断在跑的任务，拖动时新视野排在旧视野的整层扫描
后面，两次叠加就是用户看到的「标注延后 10 秒以上」。现在旧任务在批次之间按
世代号退出，且这类图层会在后台预建整层锚点表。

本脚本用真实数据量这几段，并打印拖动后标注跟随的耗时。

用法：
    .venv\\Scripts\\python.exe tools\\perf_label_latency.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parent.parent
ROADS = ROOT / "data/uploads/2415b1ef/x_zhejiang-lat/gis_osm_roads_free_1.shp"
VILLAGE = ROOT / "data/cache/L3e02056b.parquet"
PERF = ROOT / "docs/perf"


def main() -> int:
    if not ROADS.is_file():
        print("skip: 示例道路数据不在")
        return 0
    app = QApplication.instance() or QApplication(sys.argv)

    from app.engine import Layer
    from native.labels import build_label_data, build_label_table
    from native.mapview import MapView, project

    layers = []
    if VILLAGE.is_file():
        layers.append(Layer("L3e02056b", "村界", gdf=None, color="#6b7a4f",
                            cache_path=str(VILLAGE),
                            style={"label_field": "ZLDWMC", "label_size": 11}))
    roads = Layer("Lroads", "道路", gdf=None, color="#b03a2e",
                  source_path=str(ROADS), source_layer="gis_osm_roads_free_1",
                  style={"label_field": "name", "label_size": 11})
    layers.append(roads)

    mv = MapView()
    mv.resize(1200, 820)
    mv.show()
    mv.set_layers(layers)
    # 用户截图里的视野：江西南昌一带，mpp≈75.6
    x, y = project(116.47333, 28.73088)
    mv.scale = 156543.03392 * 0.8767 / (75.6 / 256.0)
    mv.tx = mv.width() / 2 - x * mv.scale
    mv.ty = mv.height() / 2 - y * mv.scale
    mv._invalidate_frame(0)
    mv.update()

    t0 = time.perf_counter()
    first_frame = first_label = None
    while time.perf_counter() - t0 < 90:
        app.processEvents()
        time.sleep(0.01)
        if first_frame is None and mv._frame is not None and not mv._vec_busy:
            first_frame = time.perf_counter() - t0
        if first_label is None and any(
                (mv._labels.get(lid) or {}).get("texts") for lid in (l.id for l in layers)):
            first_label = time.perf_counter() - t0
        if first_frame and first_label:
            break
    print(f"首帧 {first_frame or -1:.2f}s   首次出现标注 {first_label or -1:.2f}s")

    if VILLAGE.is_file():
        village = layers[0]
        t0 = time.perf_counter()
        vxy, vtexts, _va, _vr = build_label_table(village, "ZLDWMC")
        t_table = time.perf_counter() - t0
        t0 = time.perf_counter()
        build_label_data(village, "ZLDWMC", mv._view_lonlat_rect())
        t_view = time.perf_counter() - t0
        print(f"整层锚点表（一次，{len(vtexts)} 条） {t_table * 1000:6.0f} ms")
        print(f"按视口取一次（旧路径，同视野）     {t_view * 1000:6.0f} ms")
        view = mv._view_lonlat_rect()
        t0 = time.perf_counter()
        for _ in range(100):
            mv._derive_labels(village, "ZLDWMC", view)
        print(f"表建成后视口筛选一次              {(time.perf_counter() - t0) * 10:6.2f} ms")

    print("\n拖动后标注跟随（新视野的标注缓存立即可用即为 0.0s）：")
    for step in range(4):
        t_end = time.perf_counter() + 1.2
        while time.perf_counter() < t_end:
            mv.tx -= 14
            mv._invalidate_frame()
            mv.update()
            app.processEvents()
            time.sleep(0.016)
        mv._end_pan()
        t_stop = time.perf_counter()
        view = mv._view_lonlat_rect()
        best = None
        counts = {}
        while time.perf_counter() - t_stop < 15:
            app.processEvents()
            time.sleep(0.005)
            for lay in layers:
                c = mv._labels.get(lay.id) or {}
                r = c.get("rect")
                if r and r[0] <= view[0] + 1e-9 and r[1] <= view[1] + 1e-9 \
                        and r[2] >= view[2] - 1e-9 and r[3] >= view[3] - 1e-9:
                    counts[lay.name] = len(c.get("texts") or [])
                    if best is None:
                        best = time.perf_counter() - t_stop
            if len(counts) == len(layers):
                break
        print(f"  拖动 {step + 1}: 标注就绪 {('%.2fs' % best) if best is not None else '--'}"
              f"   {counts}")
        if step == 1:
            img = mv.grab().toImage()
            PERF.mkdir(parents=True, exist_ok=True)
            img.save(str(PERF / "label-drag.png"))
            print("  截图: docs/perf/label-drag.png")

    mv.shutdown()
    time.sleep(0.3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
