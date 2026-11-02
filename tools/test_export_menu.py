"""图层右键导出菜单回归：栅格不能出现矢量格式。

用法：.venv/Scripts/python.exe tools/test_export_menu.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("GIS_AGENT_PERSIST", "0")

from PySide6.QtWidgets import QApplication, QMenu

from app.raster import read_info
import geopandas as gpd
from shapely.geometry import Point

FAILS = []


def check(name, got, want=True):
    ok = (got == want) if not callable(want) else bool(want(got))
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        FAILS.append(name)


def main() -> int:
    from native.app import MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.show()
    app.processEvents()

    print("[1] 栅格导出菜单")
    ras = win.store.add_raster(
        read_info(ROOT / "samples" / "raster" / "image.tif"), "影像")
    m = QMenu()
    win._add_export_actions(m, ras, ras.id)
    texts = [a.text() for a in m.actions()]
    check("只有复制栅格文件", texts, ["导出栅格文件（复制原文件）…"])
    check("没有 GeoJSON", any("GeoJSON" in t for t in texts), False)
    check("没有 Shapefile", any("Shapefile" in t for t in texts), False)
    check("没有 CSV", any("CSV" in t for t in texts), False)

    print("[2] 矢量导出菜单保持原样")
    vec = win.store.add(gpd.GeoDataFrame(
        {"a": [1]}, geometry=[Point(104, 30)], crs=4326), "点")
    m2 = QMenu()
    win._add_export_actions(m2, vec, vec.id)
    texts2 = [a.text() for a in m2.actions()]
    check("矢量四种格式", texts2,
          ["GeoJSON", "GeoPackage", "Shapefile (zip)", "CSV 属性表"])

    win.close()
    win.deleteLater()
    app.processEvents()
    app.quit()
    print()
    if FAILS:
        print(f"结论: FAIL（{len(FAILS)} 项）：" + "、".join(FAILS))
        return 1
    print("结论: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
