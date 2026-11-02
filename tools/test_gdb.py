"""Esri FileGDB（.gdb）读取回归。

依赖 pyopenfilegdb（纯 Python 的 OpenFileGDB 实现，不依赖 GDAL）：
    pip install "pyopenfilegdb @ git+https://github.com/lizhiziwang/pyopengdb.git"

覆盖：
· .gdb 被识别为矢量数据入口（VECTOR_EXTS / 拖拽 / 目录选择）
· 多要素类全部导入，图层名是「gdb名/要素类名」
· 属性、几何、坐标系（WGS84 与 CGCS2000 3 度带）正确
· ZIP 里的 .gdb 文件夹也能认出来
· 桌面端 load_paths() 能真正把多个要素类建层

测试数据用 pyogrio(GDAL) 现场造；如果 GDAL 不支持写 OpenFileGDB 就跳过。
用法：.venv/Scripts/python.exe tools/test_gdb.py
"""
import os
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILS = []
TMP = Path(tempfile.mkdtemp(prefix="gis_gdb_test_"))


def check(name, got, want=True):
    ok = (got == want) if not callable(want) else bool(want(got))
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        FAILS.append(name)


def make_gdb() -> Path:
    """造一个含两个要素类的 GDB：一个 WGS84 点、一个 CGCS2000 3 度带面。"""
    import geopandas as gpd
    import pyogrio
    from shapely.geometry import Point, Polygon

    gdb = TMP / "demo.gdb"
    pts = gpd.GeoDataFrame(
        {"名称": ["点A", "点B"], "值": [1, 2]},
        geometry=[Point(104.0, 30.0), Point(104.1, 30.1)], crs=4326)
    poly = gpd.GeoDataFrame(
        {"name": ["面A"], "area_m2": [123.5]},
        geometry=[Polygon(
            [(39500000, 3000000), (39500000, 3000100),
             (39500100, 3000100), (39500100, 3000000)],
            holes=[[(39500020, 3000020), (39500020, 3000060),
                    (39500060, 3000060), (39500060, 3000020)]])], crs=4527)
    pyogrio.write_dataframe(pts, gdb, driver="OpenFileGDB", layer="点位")
    pyogrio.write_dataframe(poly, gdb, driver="OpenFileGDB", layer="面", append=True)
    return gdb


def pump(app, win, seconds=12):
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.processEvents()
        time.sleep(0.02)
        if not getattr(win, "_load_queue", None) and not win._busy:
            return


def main() -> int:
    try:
        import pyopenfilegdb  # noqa: F401
    except Exception as e:  # noqa: BLE001
        print(f"SKIP: 未安装 pyopenfilegdb（{e}）")
        return 0

    from app.gdb import gdb_info, gdb_layer_names, is_gdb_path, read_gdb_layers
    from app.io_utils import VECTOR_EXTS, extract_zip, file_info, read_vector

    try:
        gdb = make_gdb()
    except Exception as e:  # noqa: BLE001
        print(f"SKIP: 当前 GDAL 不能写 OpenFileGDB（{e}）")
        return 0

    print("[1] 识别与元信息")
    check("is_gdb_path", is_gdb_path(gdb), True)
    check(".gdb 在 VECTOR_EXTS 里", ".gdb" in VECTOR_EXTS, True)
    names = gdb_layer_names(gdb)
    check("列出两个要素类", names, ["点位", "面"])
    info = gdb_info(gdb)
    check("要素总数", info["features"], 3)
    check("图层数写进 geom", "2 图层" in info["geom"], True)
    check("头信息也能读", file_info(gdb)["features"], 3)

    print("[2] 多要素类读取")
    layers = read_gdb_layers(gdb)
    check("读到两个图层", [n for n, _ in layers], ["点位", "面"])
    by_name = dict(layers)
    pts, poly = by_name["点位"], by_name["面"]
    check("点图层要素数", len(pts), 2)
    check("点图层字段不含 OBJECTID", "OBJECTID" not in pts.columns, True)
    check("点图层属性正确", list(pts["名称"]), ["点A", "点B"])
    check("点图层 CRS 4326", pts.crs.to_epsg(), 4326)
    check("面图层 CRS", poly.crs.to_epsg(), 4527)
    check("面几何是 Polygon", poly.geometry.iloc[0].geom_type, "Polygon")
    check("面几何带 1 个洞", len(poly.geometry.iloc[0].interiors), 1)

    print("[3] 单图层 API + ZIP")
    try:
        read_vector(gdb)
        check("多图层 read_vector 应报错", False, True)
    except Exception as e:  # noqa: BLE001
        check("多图层 read_vector 给出中文提示", "要素类" in str(e), True)

    zpath = TMP / "gdb.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in gdb.rglob("*"):
            if f.is_file():
                z.write(f, f.relative_to(gdb.parent))
    root, files = extract_zip(zpath)
    try:
        check("ZIP 里认出 .gdb 文件夹", any(f.suffix.lower() == ".gdb" and f.is_dir()
                                           for f in files), True)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("[4] 桌面端 load_paths 多要素类建层")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from native.app import MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.resize(1000, 760)
    win.show()
    win.load_paths([str(gdb)])
    pump(app, win)
    got = [l.name for l in win.store.layers.values()]
    check("两个要素类都进图层仓库", got, ["demo/点位", "demo/面"])
    check("地图也分成两个矢量层", len(win.map._vec_layers), 2)
    face = next((l for l in win.store.layers.values() if l.name == "demo/面"), None)
    b = win.map._layer_bounds(face) if face is not None else None
    check("投影 GDB 的缩放范围转成经纬度",
          b is not None and 110 < b[0] < 125 and 20 < b[1] < 35)
    print(f"  面图层经纬度范围: {b}")
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
