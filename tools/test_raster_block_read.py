"""大窗口分块读取回归：结果必须和整窗降采样一致（nearest 逐像素一致）。

覆盖 `app.raster._read_window_blocks()`；源像元超过 400 万时才走分块，
这里造一张 3000×3000 的临时栅格。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import rasterio
from rasterio.transform import from_origin

from app.raster import RasterCancelled, RasterLayer, read_info, read_window


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gis_block_"))
    path = tmp / "block.tif"
    w = h = 3000
    yy, xx = np.mgrid[0:h, 0:w]
    data = ((xx + yy) % 251).astype("uint8")
    with rasterio.open(
        path, "w", driver="GTiff", width=w, height=h, count=1,
        dtype="uint8", crs="EPSG:4326",
        transform=from_origin(120.0, 30.0, 0.001, 0.001),
    ) as ds:
        ds.write(data, 1)

    info = read_info(path)
    lay = RasterLayer("Rblock", "block", info)
    got, _actual = read_window(lay, info.bounds, 900, 700,
                               bands=[1], resampling="nearest")
    arr = got[0]

    with rasterio.open(path) as ds:
        from rasterio.windows import from_bounds
        from rasterio.enums import Resampling
        win = from_bounds(*ds.bounds, transform=ds.transform)
        ref = ds.read(1, window=win, out_shape=(700, 900),
                      resampling=Resampling.nearest)

    assert arr.shape == ref.shape == (700, 900), (arr.shape, ref.shape)
    diff = int((arr != ref).sum())
    assert diff == 0, f"nearest 分块读取与整窗不一致: {diff} px"

    # 取消要在进入读取/块边界时立即生效（缩放时旧请求不能继续跑）。
    try:
        read_window(lay, info.bounds, 900, 700, bands=[1],
                    resampling="nearest", cancel=lambda: True)
    except RasterCancelled:
        pass
    else:
        raise AssertionError("cancel=True 时 read_window 没有中止")

    print(f"PASS  block read: shape={arr.shape} diff={diff}px cancel=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
