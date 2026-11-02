"""截「压缩包里矢量 + 栅格混合」时的导入勾选弹窗。

这条截图对应的问题：压缩包里放了 GeoTIFF，以前 extract_zip 只找矢量扩展名，
明明解压成功却报「没有找到可识别的矢量文件」；现在两类都认，且栅格那行
显示的是「尺寸 · 波段 · 坐标系」而不是「0 要素」。

真 windows 平台 + WA_DontShowOnScreen（offscreen 平台中文是豆腐块）。
用法：python tools/shot_import_dialog.py
"""
import os
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Qt 必须先于 rasterio 导入（Windows 上反过来会 access violation）
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "perf" / "import-dialog-raster.png"

app = QApplication(sys.argv)

from app.io_utils import extract_zip, file_info
from app.raster import is_raster_path, read_info
from native import dialogs


def build_candidates(zip_path: Path):
    """和 native/app.py::_load_one 里那段完全一致的候选构建逻辑。"""
    root, files = extract_zip(zip_path)
    cands = []
    for i, f in enumerate(files):
        if is_raster_path(f):
            try:
                ri = read_info(f)
                info = {"kind": "raster", "features": 0, "geom": "", "crs": ri.crs,
                        "width": ri.width, "height": ri.height, "bands": ri.bands}
            except Exception:
                info = {"kind": "raster", "features": 0, "geom": "", "crs": "",
                        "width": 0, "height": 0, "bands": 0}
        else:
            info = {"kind": "vector", **file_info(f)}
        cands.append({"id": i, "name": f.stem, "ext": f.suffix,
                      "size_mb": round(f.stat().st_size / 1e6, 3), **info})
    return cands


tmp = Path(tempfile.mkdtemp(prefix="shot_zip_"))
zp = tmp / "样例数据.zip"
with zipfile.ZipFile(zp, "w") as z:
    z.write(ROOT / "samples" / "地块.geojson", "样例/地块.geojson")
    z.write(ROOT / "samples" / "道路.geojson", "样例/道路.geojson")
    z.write(ROOT / "samples" / "raster" / "dem.tif", "样例/dem.tif")
    z.write(ROOT / "samples" / "raster" / "image.tif", "样例/image.tif")

cands = build_candidates(zp)
print("候选:", [(c["name"], c.get("kind")) for c in cands])

dlg = dialogs.ImportDialog(cands, zp.name)
dlg.setAttribute(Qt.WA_DontShowOnScreen, True)
dlg.show()
app.processEvents()
dlg.grab().save(str(OUT))
print("已保存:", OUT, OUT.stat().st_size, "字节")
