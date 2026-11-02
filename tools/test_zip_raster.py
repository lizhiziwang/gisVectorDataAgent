"""压缩包含栅格时的导入识别：`extract_zip` 必须同时认矢量和栅格。

跑法：
    PYTHONPATH="D:/gis-agent/.venv/Lib/site-packages;D:/gis-agent" \
      "D:/zsh/app/py_3.13.1/python.exe" tools/test_zip_raster.py
"""
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.io_utils import ARCHIVE_EXTS, VECTOR_EXTS, extract_zip
from app.raster import RASTER_EXTS, is_raster_path

FAIL = []


def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else f"  {extra}"))
    if not cond:
        FAIL.append(name)


def main():
    print("[1] 扩展名清单")
    check("ARCHIVE_EXTS = 矢量 + 栅格",
          set(ARCHIVE_EXTS) == set(VECTOR_EXTS) | set(RASTER_EXTS))
    check("tif 被认成栅格", is_raster_path("a.tif") and is_raster_path("A.TIF"))
    check("shp 不被认成栅格", not is_raster_path("a.shp"))

    print("[2] 纯栅格压缩包")
    src_tif = Path("samples/raster/dem.tif")
    if not src_tif.exists():
        print("  跳过：缺 samples/raster/dem.tif（先跑 tools/make_test_raster.py）")
        return
    tmp = Path(tempfile.mkdtemp(prefix="ziptest_"))
    z1 = tmp / "rasters.zip"
    with zipfile.ZipFile(z1, "w") as z:
        z.write(src_tif, "rasters/dem.tif")
        z.write(Path("samples/raster/image.tif"), "rasters/image.tif")
    root, files = extract_zip(z1)
    check("纯栅格包能列出 2 个文件", len(files) == 2, [f.name for f in files])
    check("全部是栅格", all(is_raster_path(f) for f in files))
    check("按体积降序", files[0].stat().st_size >= files[-1].stat().st_size)

    print("[3] 矢量 + 栅格混合包")
    z2 = tmp / "mixed.zip"
    with zipfile.ZipFile(z2, "w") as z:
        z.write(Path("samples/地块.geojson"), "mixed/地块.geojson")
        z.write(src_tif, "mixed/dem.tif")
    root2, files2 = extract_zip(z2)
    ext2 = {f.suffix.lower() for f in files2}
    check("混合包认到两类", ext2 == {".geojson", ".tif"}, ext2)

    print("[4] 空包 / 无可识别文件仍报错")
    z3 = tmp / "empty.zip"
    with zipfile.ZipFile(z3, "w") as z:
        z.writestr("readme.txt", "hi")
    try:
        extract_zip(z3)
        check("空包应抛错", False)
    except ValueError as e:
        check("空包报错文案提到栅格", "栅格" in str(e), str(e))

    print("[5] 导入候选的副标题格式")
    from app.raster import read_info
    import native.dialogs as dlg      # 需要 Qt 已可用
    ri = read_info(src_tif)
    line = dlg._cand_detail({"kind": "raster", "width": ri.width, "height": ri.height,
                             "bands": ri.bands, "crs": ri.crs})
    check("栅格副标题含尺寸和波段", "×" in line and "波段" in line, line)
    vline = dlg._cand_detail({"kind": "vector", "features": 12, "geom": "Polygon",
                              "crs": "EPSG:4326"})
    check("矢量副标题含要素数", "要素" in vline, vline)

    print()
    if FAIL:
        print(f"结论: FAIL（{len(FAIL)} 项）")
        sys.exit(1)
    print("结论: PASS")


if __name__ == "__main__":
    main()
