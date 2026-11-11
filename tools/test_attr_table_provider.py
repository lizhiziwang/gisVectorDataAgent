"""属性表不能为了显示属性把整层几何读进内存。

跑法：
    .venv\\Scripts\\python.exe tools\\test_attr_table_provider.py

覆盖：
1. GeoParquet / GeoJSON 的 provider 图层打开 TableDialog 时 `Layer.gdf` 仍为 None；
2. 表格只有属性列，没有 geometry；
3. 点一行还能高亮 / 定位，且不触发全量加载。
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import geopandas as gpd
from shapely.geometry import Point

from PySide6.QtWidgets import QApplication

from app.engine import Layer
from native.dialogs import TableDialog
from native.mapview import MapView

FAIL = []


def check(name, ok, extra=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"   {extra}"))
    if not ok:
        FAIL.append(name)


def make_gdf(n=12):
    return gpd.GeoDataFrame(
        {"name": [f"p{i}" for i in range(n)],
         "value": list(range(n))},
        geometry=[Point(114.9 + i * 0.001, 27.8 + i * 0.001) for i in range(n)],
        crs="EPSG:4326")


def exercise(app, path: Path, label: str):
    print(f"\n[{label}] {path.name}")
    lay = Layer("Ltest", label, gdf=None, cache_path=str(path))
    mv = MapView()
    mv.resize(640, 480)
    mv.set_layers([lay])
    mv.visible = {"Ltest": True}
    wb = lay.wgs_bounds
    mv.fit_bounds(*wb, pad=0.1)
    for _ in range(20):
        app.processEvents()

    dlg = TableDialog(lay)
    check("打开属性表后没有全量 gdf", lay._gdf is None)
    check("表格只有属性列", "geometry" not in [str(c) for c in dlg._data.columns],
          str(list(dlg._data.columns)))
    check("首页行数正确", dlg.tw.rowCount() == min(TableDialog.PAGE, len(dlg._data)),
          f"{dlg.tw.rowCount()} / {len(dlg._data)}")

    row = min(3, len(dlg._data) - 1)
    scale0 = mv.scale
    mv.highlight_feature("Ltest", row)
    mv.zoom_to_feature("Ltest", row)
    for _ in range(5):
        app.processEvents()
    check("点行后高亮", mv._hl == ("Ltest", row), str(mv._hl))
    check("点行后定位", mv.scale != scale0, f"{scale0:.0f} → {mv.scale:.0f}")
    check("点行后仍未全量加载", lay._gdf is None)

    dlg.close()
    mv.shutdown()
    mv.deleteLater()


app = QApplication.instance() or QApplication([])
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    gdf = make_gdf()

    pq = tmp / "attrs.parquet"
    gdf.to_parquet(pq, index=False, row_group_size=4)
    exercise(app, pq, "GeoParquet")

    gj = tmp / "attrs.geojson"
    gdf.to_file(gj, driver="GeoJSON")
    exercise(app, gj, "GeoJSON")

print("\n" + ("FAIL: " + ", ".join(FAIL) if FAIL else "PASS"))
sys.stdout.flush()
# Qt 的 offscreen 字体/线程清理在退出阶段常把返回码改成 1；结论已经打印，
# 这里直接按 FAIL 列表决定进程退出码，避免回归脚本误判。
os._exit(1 if FAIL else 0)
