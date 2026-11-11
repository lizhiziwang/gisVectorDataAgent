"""overview 自动管理：可见大栅格缺 overview 时后台补 .ovr，不阻塞。"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("GIS_AGENT_PERSIST", "0")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import rasterio
from rasterio.transform import from_origin
from PySide6.QtWidgets import QApplication

from app.raster import read_info
from native.app import MainWindow


def main() -> int:
    td = Path(tempfile.mkdtemp(prefix="gis_ovr_auto_"))
    path = td / "auto.tif"
    w = h = 3000
    with rasterio.open(
        path, "w", driver="GTiff", width=w, height=h, count=1,
        dtype="uint8", crs="EPSG:4326",
        transform=from_origin(100, 30, 0.001, 0.001),
    ) as ds:
        ds.write(np.random.randint(0, 255, (1, h, w), dtype=np.uint8))

    app = QApplication.instance() or QApplication([])  # noqa: F841
    win = MainWindow()
    win.resize(900, 640)
    win.show()
    lay = win.store.add_raster(read_info(path), "auto")
    win.visible = {lay.id: True}
    win.map.visible = win.visible
    win.refresh_layers(fit=True)

    ovr = Path(str(path) + ".ovr")
    deadline = time.time() + 30
    while time.time() < deadline and not ovr.is_file():
        app.processEvents()
        time.sleep(0.03)
    for _ in range(20):
        app.processEvents()
        time.sleep(0.02)
    ok = ovr.is_file() and bool(lay.info.overviews)
    print(f"  {'OK ' if ok else '!! '} 自动生成 .ovr: {ovr.is_file()}，"
          f"overviews={lay.info.overviews}")
    try:
        win.map.shutdown()
    except Exception:  # noqa: BLE001
        pass
    win.close()
    win.deleteLater()
    app.processEvents()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
