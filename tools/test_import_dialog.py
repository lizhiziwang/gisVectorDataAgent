"""自定义导入弹窗回归：GDB / GPKG 内部图层都要能列出来、能只导所选。

覆盖：
· 点「添加 GDB 文件夹」列出多个要素类；
· 点「添加文件」选 GPKG 列出多个图层；
· 勾选/全不选得到正确的 selections；
· MainWindow 按 selections 只导入勾选的图层（GDB / GPKG）。

用法：.venv/Scripts/python.exe tools/test_import_dialog.py
"""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="gis_import_dialog_"))
FAILS = []


def check(name, got, want=True):
    ok = (got == want) if not callable(want) else bool(want(got))
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        FAILS.append(name)


def make_data():
    import geopandas as gpd
    import pyogrio
    from shapely.geometry import Point, Polygon

    gdb = TMP / "demo.gdb"
    pts = gpd.GeoDataFrame({"名称": ["A", "B"]}, geometry=[Point(104, 30), Point(104.1, 30.1)],
                           crs=4326)
    poly = gpd.GeoDataFrame(
        {"name": ["P"]},
        geometry=[Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])], crs=4326)
    pyogrio.write_dataframe(pts, gdb, driver="OpenFileGDB", layer="点位")
    pyogrio.write_dataframe(poly, gdb, driver="OpenFileGDB", layer="面", append=True)

    gpkg = TMP / "demo.gpkg"
    a = gpd.GeoDataFrame({"v": [1]}, geometry=[Point(1, 2)], crs=4326)
    b = gpd.GeoDataFrame({"v": [2]}, geometry=[Point(3, 4)], crs=4326)
    pyogrio.write_dataframe(a, gpkg, driver="GPKG", layer="点一")
    pyogrio.write_dataframe(b, gpkg, driver="GPKG", layer="点二", append=True)
    return gdb, gpkg


def pump(app, win, sec=8):
    t0 = time.time()
    while time.time() - t0 < sec:
        app.processEvents()
        time.sleep(0.02)
        if not getattr(win, "_load_queue", None) and not win._busy:
            return


def main() -> int:
    gdb, gpkg = make_data()
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from native.app import MainWindow
    from native.dialogs import ImportDataDialog

    app = QApplication.instance() or QApplication([])
    dlg = ImportDataDialog(start_paths=[str(gdb), str(gpkg)])
    entries = dlg._entries
    names = [e["name"] for e in entries]
    print("[1] 列图层", names)
    check("GDB 列出 2 个图层", sum(1 for n in names if n.startswith("demo/")) >= 3, True)
    check("GPKG 列出 2 个图层", sum(1 for n in names if n.startswith("demo/点")) >= 2, True)
    check("默认全选", len(dlg.selections()) == len(entries), True)

    # 只选 GDB 的「面」和 GPKG 的「点二」
    for cb, e in dlg._boxes:
        cb.setChecked(e["name"] in ("demo/面", "demo/点二"))
    picked = dlg.selections()
    print("[2] 勾选", [e["name"] for e in picked])
    check("只选中 2 个", len(picked), 2)
    check("选中的目标正确",
          sorted(e["name"] for e in picked), ["demo/点二", "demo/面"])

    print("[3] 全不选/全选按钮")
    dlg._set_all(False)
    check("全不选", dlg.selections(), [])
    check("全不选时导入按钮禁用", dlg._ok.isEnabled(), False)
    dlg._set_all(True)
    check("全选", len(dlg.selections()), len(entries))
    dlg.deleteLater()

    print("[4] 按 selections 只导入勾选图层")
    win = MainWindow()
    win.resize(1000, 760)
    win.show()
    files = []
    selections = {}
    for e in picked:
        p = e["path"]
        if p not in files:
            files.append(Path(p))
        if e.get("layer"):
            selections.setdefault(p, []).append(e["layer"])
    win._load_files(files, source="测试导入", selections=selections)
    pump(app, win)
    got = sorted(l.name for l in win.store.layers.values())
    print("  载入图层", got)
    check("只建了 2 个图层", len(got), 2)
    check("GDB 只导了面", "demo/面" in got, True)
    check("GDB 没导点位", "demo/点位" in got, False)
    check("GPKG 只导了点二", "demo/点二" in got, True)
    check("GPKG 没导点一", "demo/点一" in got, False)
    win.close()
    app.processEvents()

    print()
    if FAILS:
        print(f"结论: FAIL（{len(FAILS)} 项）：" + "、".join(FAILS))
        return 1
    print("结论: PASS")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
