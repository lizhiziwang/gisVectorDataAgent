"""栅格裁剪子进程：把 GDAL/rasterio 的重活隔离出 UI 进程。

主进程里的 Agent 任务线程即使把工作交给 rasterio，GDAL/压缩仍可能长时间
不释放 GIL，Qt 主线程就会“未响应”。这里用独立子进程跑裁剪，主进程只
等待子进程退出，GIL 与内存压力都不会传导到界面。

输入是一份 JSON 配置，输出 GeoTIFF；不依赖 Qt。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.features import geometry_mask, geometry_window
from rasterio.warp import transform_bounds
from rasterio.windows import Window, from_bounds


def _fill_nodata(src, nodata, dtype):
    if nodata is not None:
        return nodata
    if src.nodata is not None:
        return src.nodata
    return np.nan if np.dtype(dtype).kind == "f" else 0


def _profile(src, count, dtype, width, height, transform, nd):
    return {
        "driver": "GTiff",
        "width": int(width),
        "height": int(height),
        "count": int(count),
        "dtype": np.dtype(dtype).name,
        "crs": src.crs,
        "transform": transform,
        "nodata": nd,
        "tiled": True,
        "blockxsize": 256,
        "blockysize": 256,
        "compress": "deflate",
        "BIGTIFF": "IF_SAFER",
    }


def clip(cfg: dict) -> dict:
    raster = str(cfg["raster"])
    out = str(cfg["out"])
    block = int(cfg.get("block") or 1024)
    shapes = None
    with rasterio.open(raster) as src:
        if cfg.get("mask_geojson"):
            gdf = gpd.read_file(cfg["mask_geojson"])
            if gdf.crs is None:
                raise ValueError("裁剪面没有坐标系，无法和栅格对齐。")
            if src.crs is not None and gdf.crs.to_string() != src.crs.to_string():
                gdf = gdf.to_crs(src.crs.to_string())
            from shapely.geometry import mapping
            shapes = [mapping(g) for g in gdf.geometry
                      if g is not None and not g.is_empty]
            if not shapes:
                raise ValueError("裁剪面没有有效几何。")
            try:
                win = geometry_window(src, shapes, pad_x=0, pad_y=0)
            except Exception as e:  # noqa: BLE001
                raise ValueError(f"裁剪面与栅格范围没有交集：{e}") from e
        else:
            x0, y0, x1, y1 = [float(v) for v in cfg["bbox"]]
            if src.crs is None:
                raise ValueError("栅格没有坐标系，无法换算 bbox。")
            rb = (x0, y0, x1, y1)
            if src.crs.to_epsg() != 4326:
                rb = transform_bounds("EPSG:4326", src.crs, x0, y0, x1, y1,
                                      densify_pts=21)
            win = from_bounds(rb[0], rb[1], rb[2], rb[3], transform=src.transform)

        win = win.round_offsets().round_lengths()
        try:
            win = win.intersection(Window(0, 0, src.width, src.height))
        except Exception as e:  # noqa: BLE001
            raise ValueError("裁剪范围与栅格没有交集。") from e
        if win.width < 1 or win.height < 1:
            raise ValueError("裁剪范围与栅格没有交集（或小于一个像元）。")
        col, row = int(win.col_off), int(win.row_off)
        width, height = int(win.width), int(win.height)
        transform = src.window_transform(win)
        nd = _fill_nodata(src, cfg.get("nodata"), src.dtypes[0])
        dtype = np.dtype(src.dtypes[0])
        prof = _profile(src, src.count, dtype, width, height, transform, nd)
        with rasterio.open(out, "w", **prof) as dst:
            for row0 in range(0, height, block):
                h = min(block, height - row0)
                for col0 in range(0, width, block):
                    w = min(block, width - col0)
                    data = src.read(window=Window(col + col0, row + row0, w, h))
                    if shapes:
                        valid = geometry_mask(
                            shapes, out_shape=(h, w),
                            transform=dst.window_transform(Window(col0, row0, w, h)),
                            all_touched=bool(cfg.get("all_touched")), invert=True)
                        if not valid.all():
                            data[:, ~valid] = nd
                    dst.write(data, window=Window(col0, row0, w, h))
    return {"width": width, "height": height}


def main() -> int:
    if len(sys.argv) < 2:
        return 2
    result_path = ""
    try:
        cfg = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        result_path = str(cfg.get("result") or "")
        res = clip(cfg)
        if result_path:
            Path(result_path).write_text(
                json.dumps({"ok": True, **res}, ensure_ascii=False),
                encoding="utf-8")
        return 0
    except Exception as e:  # noqa: BLE001
        if result_path:
            try:
                Path(result_path).write_text(
                    json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"},
                               ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
