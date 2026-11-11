"""GeoParquet provider 回归：文件头元信息 + 按 batch 读 + 按需 bounds。

对应 app/vector_provider.py 里的 parquet 分支。这里刻意写多个 row group，
保证 iter_batches 真的按 batch 产出，而不是一次读完整层。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import geopandas as gpd
from shapely.geometry import box

from app.vector_provider import VectorProvider


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gis_parquet_"))
    path = tmp / "cache.parquet"
    geoms = [box(i * 0.01, j * 0.01, i * 0.01 + 0.008, j * 0.01 + 0.008)
             for i in range(40) for j in range(10)]
    gdf = gpd.GeoDataFrame(
        {"fid": np.arange(len(geoms)), "name": [f"p{i}" for i in range(len(geoms))]},
        geometry=geoms, crs="EPSG:4326")
    gdf.to_parquet(path, index=False, row_group_size=50)

    p = VectorProvider(path)
    assert p.count == 400, p.count
    assert p.crs is not None and p.crs.to_epsg() == 4326, p.crs
    assert p.fields == ["fid", "name"], p.fields
    assert p.bounds == (), p.bounds          # 打开时不扫几何
    assert p.wgs_bounds is not None and len(p.wgs_bounds) == 4

    batches = list(p.iter_bbox((-1, -1, 0.05, 0.05), batch_size=4096))
    assert batches, "bbox 内应有要素"
    total = sum(len(b) for b in batches)
    assert total > 0, total
    assert all(len(b) <= 512 for b in batches), [len(b) for b in batches]

    bbox_gdf = p.read_bbox((-1, -1, 0.05, 0.05))
    assert isinstance(bbox_gdf, gpd.GeoDataFrame) and len(bbox_gdf) > 0
    assert bbox_gdf.crs is not None and bbox_gdf.crs.to_epsg() == 4326

    full = p.read_all()
    assert isinstance(full, gpd.GeoDataFrame) and len(full) == 400

    # 恢复会话时只读元信息：Layer.meta() 不能为了 count 触发全量读取。
    from app.engine import Layer
    lay = Layer("Ltest", "cache", gdf=None, cache_path=str(path))
    meta = lay.meta()
    assert meta["count"] == 400, meta
    assert lay._gdf is None, "Layer.meta() 不应把 parquet 全量读进内存"

    print(f"PASS  parquet provider: count={p.count} batches={len(batches)} "
          f"bbox_features={total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
