"""投影坐标 GeoParquet 的矢量渲染 LOD 缓存回归。

跑法：
    .venv\\Scripts\\python.exe tools\\test_vector_lod_cache.py

检查：
1. 第一次 `iter_bbox(..., simplify_tol=...)` 会建立整层简化几何缓存；
2. 同一档位的第二次调用 / 平移只做 bbox 过滤，结果仍在视口附近；
3. 缓存只影响渲染，`read_bbox()` 仍返回完整顶点，不把简化写回数据。
"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import geopandas as gpd
import numpy as np
import shapely
from shapely.geometry import Point

from app.vector_provider import provider_for

FAIL = []


def check(name, ok, extra=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"   {extra}"))
    if not ok:
        FAIL.append(name)


def make_layer(path: Path) -> None:
    geoms = []
    for i in range(24):
        x = 500000.0 + (i % 6) * 12000.0
        y = 3000000.0 + (i // 6) * 12000.0
        # 128 段圆：顶点足够多，简化后能明显减少
        geoms.append(Point(x, y).buffer(5000.0, resolution=64))
    gdf = gpd.GeoDataFrame({"name": [f"p{i}" for i in range(len(geoms))]},
                           geometry=geoms, crs="EPSG:4547")
    gdf.to_parquet(path, index=False, row_group_size=2)


def count_vertices(parts) -> int:
    if not parts:
        return 0
    arr = np.concatenate(parts)
    return int(shapely.get_num_coordinates(arr).sum())


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gis-lod-"))
    path = tmp / "lod.parquet"
    make_layer(path)
    p = provider_for(str(path))
    wb = p.wgs_bounds
    check("拿到 WGS 范围", bool(wb and len(wb) == 4), f"{wb}")
    check("WGS 范围写入旁车", Path(str(path) + ".bounds.json").exists())
    q = provider_for(str(path))
    check("重开直接读旁车范围", q.wgs_bounds_hint == wb,
          f"{q.wgs_bounds_hint} != {wb}")

    tol = 0.001  # 约 100m；圈多边形保留轮廓，但顶点会减少
    t0 = time.perf_counter()
    first = list(p.iter_bbox(wb, batch_size=512, simplify_tol=tol))
    dt1 = time.perf_counter() - t0
    n1 = sum(len(x) for x in first)
    check("首次渲染有要素", n1 > 0, f"n={n1}")
    check("建立了简化缓存", len(p._render_cache) >= 1,
          f"keys={list(p._render_cache)}")
    cached_vertices = count_vertices(first)

    t0 = time.perf_counter()
    second = list(p.iter_bbox(wb, batch_size=512, simplify_tol=tol))
    dt2 = time.perf_counter() - t0
    n2 = sum(len(x) for x in second)
    check("同档位复用缓存", n1 == n2, f"{n1} vs {n2}")
    check("缓存命中更快", dt2 <= max(0.05, dt1 * 0.9),
          f"first={dt1*1000:.0f}ms second={dt2*1000:.0f}ms")

    # 平移视口：只取左上一小块，不应该把整层要素都返回
    x0, y0, x1, y1 = wb
    small = (x0, y1 - (y1 - y0) * 0.25, x0 + (x1 - x0) * 0.25, y1)
    pan = list(p.iter_bbox(small, batch_size=512, simplify_tol=tol))
    npan = sum(len(x) for x in pan)
    check("平移按 bbox 过滤", 0 < npan < n2, f"pan={npan} all={n2}")

    full = p.read_bbox(wb)
    full_vertices = int(shapely.get_num_coordinates(full.geometry.values).sum())
    check("read_bbox 仍是完整几何", full_vertices > cached_vertices,
          f"full={full_vertices} cached={cached_vertices}")

    # 点击命中：只读属性列，不触发 gpd.read_parquet / 全量几何
    row0 = full.geometry.iloc[0]
    c = row0.centroid
    hit = p.hit_test(c.x, c.y, tol=0.01)
    check("provider 命中要素", hit is not None)
    if hit is not None:
        check("命中返回属性", "name" in (hit[1] or {}), f"props={hit[1]}")
    check("属性表不含几何列",
          p._attrs_table is None or "geometry" not in p._attrs_table.column_names)

    print("\n" + ("FAIL: " + ", ".join(FAIL) if FAIL else "PASS"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
