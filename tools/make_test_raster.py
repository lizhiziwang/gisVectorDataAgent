"""生成测试用的栅格数据（不联网）。

造三份覆盖成都片区的栅格，覆盖渲染管线的三条主要分支：
· `dem.tif`     单波段浮点 + nodata，模拟高程图（走灰度拉伸）
· `image.tif`   三波段 8 位，模拟卫星影像（走 RGB）
· `crop_small.tif` 只覆盖视口一小块，验证「栅格比视口小」时的定位是否正确

用法：python tools/make_test_raster.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import rasterio
from rasterio.transform import from_bounds

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "samples", "raster")

# 成都片区，和 samples/*.geojson 大致同一范围
BOUNDS = (103.95, 30.55, 104.15, 30.72)


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)

    # ---- 1. 单波段浮点高程（带 nodata）
    w, h = 600, 480
    yy, xx = np.mgrid[0:h, 0:w]
    dem = (500 + 180 * np.sin(xx / 40.0) * np.cos(yy / 55.0)
           + 60 * np.sin(xx / 11.0)).astype(np.float32)
    dem[0:30, 0:30] = -9999.0            # 一块 nodata，验证透明
    p = os.path.join(OUT_DIR, "dem.tif")
    with rasterio.open(p, "w", driver="GTiff", width=w, height=h, count=1,
                       dtype="float32", crs="EPSG:4326",
                       transform=from_bounds(*BOUNDS, w, h),
                       nodata=-9999.0, compress="deflate") as ds:
        ds.write(dem, 1)
    print("已生成:", p, f"{os.path.getsize(p)} 字节")

    # ---- 2. 三波段 8 位影像
    r = (np.sin(xx / 60.0) * 0.5 + 0.5) * 255
    g = (np.cos(yy / 70.0) * 0.5 + 0.5) * 255
    b = ((xx + yy) / float(w + h)) * 255
    rgb = np.stack([r, g, b]).astype(np.uint8)
    p2 = os.path.join(OUT_DIR, "image.tif")
    with rasterio.open(p2, "w", driver="GTiff", width=w, height=h, count=3,
                       dtype="uint8", crs="EPSG:4326",
                       transform=from_bounds(*BOUNDS, w, h),
                       compress="deflate") as ds:
        ds.write(rgb)
    print("已生成:", p2, f"{os.path.getsize(p2)} 字节")

    # ---- 3. 小块栅格（只覆盖视口一角）
    small = (103.98, 30.58, 104.02, 30.61)
    w2, h2 = 120, 90
    blob = np.zeros((h2, w2), dtype=np.uint8)
    yy2, xx2 = np.mgrid[0:h2, 0:w2]
    blob[:] = ((np.sin(xx2 / 8.0) * 0.5 + 0.5) * 255).astype(np.uint8)
    p3 = os.path.join(OUT_DIR, "crop_small.tif")
    with rasterio.open(p3, "w", driver="GTiff", width=w2, height=h2, count=1,
                       dtype="uint8", crs="EPSG:4326",
                       transform=from_bounds(*small, w2, h2), compress="deflate") as ds:
        ds.write(blob, 1)
    print("已生成:", p3, f"{os.path.getsize(p3)} 字节")
    return 0


if __name__ == "__main__":
    sys.exit(main())
