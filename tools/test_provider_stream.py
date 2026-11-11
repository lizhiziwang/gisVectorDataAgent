"""provider 的惰性 Arrow 流：不物化整个 bbox，首批要素早于全量返回。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.vector_provider import VectorProvider  # noqa: E402

WATER = (ROOT / "data/uploads/2415b1ef/x_zhejiang-lat/"
         "gis_osm_water_a_free_1.shp")


def main() -> int:
    if not WATER.is_file():
        print("skip: sample missing")
        return 0
    prov = VectorProvider(WATER)
    t0 = time.perf_counter()
    first = None
    total = 0
    batches = 0
    for geoms in prov.iter_bbox(prov.wgs_bounds, batch_size=4096):
        if first is None:
            first = time.perf_counter() - t0
        total += len(geoms)
        batches += 1
    dt = time.perf_counter() - t0
    ok = (
        total == prov.count
        and batches > 1
        and first is not None
        and first < dt * 0.9
    )
    print(f"要素 {total}/{prov.count}，批次数 {batches}，"
          f"首批 {first * 1000:.0f} ms，总 {dt * 1000:.0f} ms")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
