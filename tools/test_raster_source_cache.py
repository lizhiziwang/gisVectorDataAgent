"""源 block cache 回归：选择 overview、跨缩放复用、与整窗结果接近。"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from app.raster import RasterError, RasterLayer, read_info, select_overview_index
from native.mapview import MapView
from native.rasterview import (RasterViewPort, SOURCE_CACHE, TILE_CACHE,
                               _render_layer_image_full, render_layer_image)


def _arr(img: QImage) -> np.ndarray:
    img = img.convertToFormat(QImage.Format_RGBA8888)
    h, w = img.height(), img.width()
    return np.frombuffer(img.constBits(), np.uint8,
                         img.bytesPerLine() * h).reshape(
        h, img.bytesPerLine() // 4, 4)[:, :w].copy()


def main() -> int:
    app = QApplication.instance() or QApplication([])  # noqa: F841
    td = Path(tempfile.mkdtemp(prefix="gis_srcblock_"))
    path = td / "src_blocks.tif"
    w = h = 6000
    yy, xx = np.mgrid[0:h, 0:w]
    # 平滑的假彩色图：源块和整窗只应差在重采样相位，不应出现整块错位。
    rgb = np.stack([
        (xx / w * 180 + yy / h * 50),
        (xx / w * 40 + 80),
        (yy / h * 160 + 30),
    ]).astype("uint8")
    with rasterio.open(
        path, "w", driver="GTiff", width=w, height=h, count=3,
        dtype="uint8", crs="EPSG:4326",
        transform=from_origin(120.0, 30.0, 0.0005, 0.0005),
    ) as ds:
        ds.write(rgb)
        ds.build_overviews([2, 4, 8], Resampling.average)

    info = read_info(path)
    assert info.overviews == (2, 4, 8), info.overviews
    assert select_overview_index(info.overviews, 8.0) == 2
    assert select_overview_index(info.overviews, 1.2) is None

    lay = RasterLayer("Rsrc", "src", info)
    mv = MapView()
    mv.resize(900, 640)
    mv.show()
    app.processEvents()
    mv.set_layers([lay])
    mv.fit_all()
    app.processEvents()

    dpr = mv._device_pixel_ratio()
    def vp():
        return RasterViewPort(
            mv._view_lonlat_rect(), max(1, int(mv.width() * dpr)),
            max(1, int(mv.height() * dpr)), mv.scale, mv.tx, mv.ty, dpr,
            mv._view_world_rect())

    SOURCE_CACHE.clear()
    TILE_CACHE.clear()
    lay.stats = None
    os.environ["GIS_AGENT_RASTER_BLOCKS"] = "1"
    try:
        first = render_layer_image(lay, vp())
        s1 = SOURCE_CACHE.stats()
        assert first is not None and s1["blocks"] > 0, s1
        second = render_layer_image(lay, vp())
        s2 = SOURCE_CACHE.stats()
        assert s2["hits"] > s1["hits"], (s1, s2)

        # 缩放一级：新 overview 层的块进入缓存，不重新整窗读。
        mv.zoom_at(2.0)
        app.processEvents()
        zoomed = render_layer_image(lay, vp())
        s3 = SOURCE_CACHE.stats()
        assert zoomed is not None and s3["blocks"] > s2["blocks"], (s2, s3)

        # 和整窗路径比较：允许重采样差异，不允许整块错位/变黑。
        SOURCE_CACHE.clear()
        a = render_layer_image(lay, vp())
        b = _render_layer_image_full(lay, vp())
        assert a is not None and b is not None
        da, db = _arr(a), _arr(b)
        diff = np.abs(da.astype(int) - db.astype(int))
        mean = float(diff.mean())
        assert mean < 3.0, f"source block 与整窗差异过大: mean={mean}"
        print(f"PASS  source block: first={s1} second={s2} zoom={s3} "
              f"mean_diff={mean:.2f}")
    finally:
        os.environ.pop("GIS_AGENT_RASTER_BLOCKS", None)
        # MapView 自带渲染线程；不主动停掉的话，解释器退出时线程还在跑，
        # Qt 会 qFatal「QThread: Destroyed while thread is still running」，
        # 退出码变成 0xC0000409 —— 断言全过也算失败。
        try:
            mv.shutdown()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
